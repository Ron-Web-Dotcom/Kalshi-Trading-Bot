"""Job: execute paper (or live) trades — full pipeline with detailed logging."""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("trading.jobs.trade")


@dataclass
class TradingResults:
    total_positions: int = 0
    total_capital_used: float = 0.0
    capital_efficiency: float = 0.0
    expected_annual_return: float = 0.0
    arb_trades: int = 0
    ai_trades: int = 0
    skipped: int = 0


async def run_trading_job(db=None) -> TradingResults:
    """
    One full trading cycle:

      1. Load cached markets from DB
      2. Polymarket comparison → cross-market arb signals
      3. Internal arb detection (YES+NO < 100¢)
      4. Execute arb signals directly  (math guarantees edge; no AI needed)
      5. AI decisions on remaining top-volume markets
      6. Risk gate on every AI trade
      7. Log every skip with reason

    All price values are in CENTS (0–99) throughout this module.
    """
    from src.config.settings import settings
    from src.data.market_data import MarketDataFetcher
    from src.data.external_markets import ExternalMarketComparator
    from src.strategy.arbitrage import ArbitrageDetector
    from src.jobs.decide import make_decision_for_market
    from src.execution.paper_trader import PaperTrader
    from src.risk.manager import RiskManager
    from src.risk.scaling import AutoScaler
    from src.alerts.discord import DiscordAlerter
    from src.clients.kalshi_client import KalshiClient
    from src.utils.database import DatabaseManager

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    live_mode    = settings.trading.live_trading_enabled
    max_trades   = settings.trading.max_trades_per_cycle
    max_scan     = settings.trading.max_markets_to_scan
    min_vol      = settings.trading.min_market_volume
    portfolio_val = settings.trading.portfolio_value

    kalshi    = KalshiClient()
    fetcher   = MarketDataFetcher(kalshi, db)
    comparator = ExternalMarketComparator(db)
    arb       = ArbitrageDetector()
    risk      = RiskManager(db)
    scaler    = AutoScaler()
    discord   = DiscordAlerter()
    results   = TradingResults()
    trades_this_cycle = 0

    mode_label = "LIVE" if live_mode else "PAPER"

    # Build trader
    if live_mode:
        from src.execution.live_trader import LiveTrader
        trader = LiveTrader(kalshi=kalshi, db=db, discord=discord,
                            scaler=scaler, risk=risk)
    else:
        trader = PaperTrader(db=db, discord=discord, scaler=scaler, risk=risk)

    logger.info("━━━ TRADING CYCLE START (%s) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", mode_label)

    try:
        # ── 1. Load markets ───────────────────────────────────────────────────
        markets = await fetcher.get_cached_markets(min_volume=min_vol)
        if not markets:
            logger.warning("No markets in DB (run ingest first) — cycle skipped")
            return results
        logger.info("Markets loaded: %d available (volume ≥ %g)", len(markets), min_vol)

        market_map = {m["ticker"]: m for m in markets}

        # ── 2 & 3. Arbitrage detection ────────────────────────────────────────
        logger.info("── Arbitrage Scan ──────────────────────────────────────────")
        ext_comps     = await comparator.compare_and_log(markets)
        cross_signals = arb.detect(ext_comps)
        int_signals   = arb.detect_internal(markets)
        all_signals   = cross_signals + int_signals

        logger.info(
            "Arb signals found: %d cross-market, %d internal  (total=%d)",
            len(cross_signals), len(int_signals), len(all_signals),
        )

        # ── 4. Execute arb signals ────────────────────────────────────────────
        if all_signals:
            logger.info("── Arb Execution ──────────────────────────────────────────")

        for sig in all_signals:
            if trades_this_cycle >= max_trades:
                logger.info("Trade cap (%d) reached — stopping arb execution", max_trades)
                break

            ticker  = sig["ticker"]
            market  = market_map.get(ticker)
            src     = sig["signal_source"]

            if not market:
                logger.warning("SKIP arb %s — not in cached markets", ticker)
                results.skipped += 1
                continue

            if src == "internal_arb":
                # ── Both legs: BUY YES + BUY NO ──────────────────────────────
                yes_p = sig["yes_price"]   # cents
                no_p  = sig["no_price"]    # cents
                net   = sig["edge_cents"]
                logger.info(
                    "INTERNAL ARB %s | YES=%g¢ + NO=%g¢ = %g¢ | Net edge=%.1f¢",
                    ticker, yes_p, no_p, yes_p + no_p, net,
                )
                for side, price in [("yes", yes_p), ("no", no_p)]:
                    allowed, reason = risk.check_trade(
                        ticker + f"_{side}", scaler.current_size,
                        current_positions=[], portfolio_value=portfolio_val,
                    )
                    if not allowed:
                        logger.info(
                            "SKIP internal-arb %s leg=%s | Reason: %s",
                            ticker, side, reason,
                        )
                        results.skipped += 1
                        continue
                    rec = await trader.execute(
                        ticker=ticker, action="BUY", side=side,
                        price_cents=price, ai_confidence=99.0,
                        ai_reasoning=(
                            f"Internal arb: YES+NO={yes_p+no_p:.0f}¢ "
                            f"(should be 100¢). Net edge after fees={net:.1f}¢"
                        ),
                        signal_source="internal_arb",
                    )
                    if rec:
                        trades_this_cycle += 1
                        results.total_positions += 1
                        results.total_capital_used += rec.get("total_cost", 0)
                        results.arb_trades += 1
                        if discord.cfg.alert_on_signal:
                            await discord.arb_signal(
                                ticker=ticker, signal_type="internal_arb",
                                gross_edge=sig["gross_edge_cents"],
                                net_edge=net,
                            )

            else:
                # ── Cross-market: single determined side ──────────────────────
                side  = sig.get("side", "yes")
                price = market.get(f"{side}_ask", 0)
                net   = sig["edge_cents"]

                if price <= 0 or price >= 100:
                    logger.info(
                        "SKIP cross-arb %s | Price %.0f¢ out of range", ticker, price
                    )
                    results.skipped += 1
                    continue

                logger.info(
                    "CROSS-MARKET ARB %s | BUY %s @ %.0f¢ | "
                    "Kalshi=%.0f¢ Poly=%.0f¢ | Net edge=%.1f¢",
                    ticker, side.upper(), price,
                    sig["kalshi_price"], sig["poly_price"], net,
                )

                allowed, reason = risk.check_trade(
                    ticker, scaler.current_size,
                    current_positions=[], portfolio_value=portfolio_val,
                )
                if not allowed:
                    logger.info("SKIP cross-arb %s | Reason: %s", ticker, reason)
                    results.skipped += 1
                    continue

                rec = await trader.execute(
                    ticker=ticker, action="BUY", side=side,
                    price_cents=price, ai_confidence=95.0,
                    ai_reasoning=(
                        f"Cross-market arb: Kalshi={sig['kalshi_price']:.0f}¢ "
                        f"vs Poly={sig['poly_price']:.0f}¢. "
                        f"Net edge after fee={net:.1f}¢"
                    ),
                    signal_source="cross_market_arb",
                )
                if rec:
                    trades_this_cycle += 1
                    results.total_positions += 1
                    results.total_capital_used += rec.get("total_cost", 0)
                    results.arb_trades += 1
                    if discord.cfg.alert_on_signal:
                        await discord.arb_signal(
                            ticker=ticker, signal_type="cross_market_arb",
                            gross_edge=sig["gross_edge_cents"],
                            net_edge=net,
                            side=side,
                            kalshi_price=sig["kalshi_price"],
                            poly_price=sig["poly_price"],
                        )

        # ── 5. AI decisions on top non-arb markets ────────────────────────────
        arb_tickers = {s["ticker"] for s in all_signals}

        # Smart pre-filter and scoring before AI scan
        # Score = volume-weighted tradability; exclude near-certain and dead markets
        def _score_candidate(m: Dict) -> float:
            yes_ask = m.get("yes_ask", 0)
            no_ask  = m.get("no_ask", 0)
            volume  = m.get("volume", 0)
            if yes_ask <= 5 or yes_ask >= 95:   # near-certain → skip
                return -1.0
            if no_ask <= 5 or no_ask >= 95:
                return -1.0
            if volume < min_vol:
                return -1.0
            # Markets near 50¢ have most uncertainty = most opportunity
            distance_from_50 = abs(yes_ask - 50)
            uncertainty_bonus = max(0, 25 - distance_from_50)   # 0–25
            return volume * 0.001 + uncertainty_bonus

        candidates = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and _score_candidate(m) > 0
        ]
        candidates.sort(key=_score_candidate, reverse=True)
        candidates = candidates[:max_scan]

        logger.info(
            "── AI Decisions (%d scored candidates, cap=%d remaining) ─────",
            len(candidates), max_trades - trades_this_cycle,
        )

        for market in candidates:
            if trades_this_cycle >= max_trades:
                logger.info("Trade cap (%d) reached — stopping AI scan", max_trades)
                break

            ticker  = market.get("ticker", "")
            yes_ask = market.get("yes_ask", 0)
            no_ask  = market.get("no_ask", 0)
            volume  = market.get("volume", 0)
            title   = (market.get("title") or "")[:50]

            logger.debug(
                "AI eval: %-30s | YES=%g¢ NO=%g¢ | vol=%g | %s",
                ticker, yes_ask, no_ask, volume, title,
            )

            decision = await make_decision_for_market(market, all_signals, db=db)
            if not decision:
                logger.info(
                    "SKIP %s | AI → HOLD or conf < %.0f%%",
                    ticker, settings.trading.min_ai_confidence,
                )
                results.skipped += 1
                continue

            # Use the side the AI chose; pick correct ask price for that side
            side  = decision.get("side", "yes")
            price = yes_ask if side == "yes" else no_ask
            net_ev = decision.get("net_ev")
            ev_str = f" | EV={net_ev:.1f}¢" if net_ev is not None else ""

            # Final EV sanity check on actual price
            if price <= 0 or price >= 100:
                logger.info("SKIP %s | %s ask=%.0f¢ out of range", ticker, side.upper(), price)
                results.skipped += 1
                continue

            # ── Profit gate: require minimum ROI AND minimum dollar profit ──────
            # Estimate expected profit based on planned position size
            planned_size_usd = scaler.current_size   # dollars to deploy
            contracts_est    = (planned_size_usd / (price / 100)) if price > 0 else 0
            exp_profit_usd   = contracts_est * (net_ev / 100) if net_ev is not None else None
            roi_pct          = (exp_profit_usd / planned_size_usd * 100) if (exp_profit_usd and planned_size_usd) else None

            min_roi = settings.trading.min_profit_roi_pct
            min_abs = settings.trading.min_profit_abs_usd

            if exp_profit_usd is not None and roi_pct is not None:
                if exp_profit_usd < min_abs or roi_pct < min_roi:
                    logger.info(
                        "SKIP %s | Profit gate: expected $%.2f (%.1f%% ROI) < min $%.2f / %.1f%%",
                        ticker, exp_profit_usd, roi_pct, min_abs, min_roi,
                    )
                    results.skipped += 1
                    continue
                ev_str += f" | exp_profit=${exp_profit_usd:.2f} ({roi_pct:.1f}% ROI)"

            logger.info(
                "AI signal: %s → BUY %s @ %.0f¢ | conf=%.0f%%%s | %s",
                ticker, side.upper(), price, decision["confidence"], ev_str,
                decision["reasoning"][:80],
            )

            allowed, reason = risk.check_trade(
                ticker, scaler.current_size,
                current_positions=[], portfolio_value=portfolio_val,
            )
            if not allowed:
                logger.info("SKIP %s | Risk gate: %s", ticker, reason)
                results.skipped += 1
                continue

            rec = await trader.execute(
                ticker=ticker,
                action=decision["action"],
                side=side,
                price_cents=price,
                ai_confidence=decision["confidence"],
                ai_reasoning=decision["reasoning"],
                signal_source=decision.get("model", "ai"),
            )
            if rec:
                trades_this_cycle += 1
                results.total_positions += 1
                results.total_capital_used += rec.get("total_cost", 0)
                results.ai_trades += 1

    except Exception as e:
        logger.error("Trade job crashed: %s", e, exc_info=True)
        try:
            await discord.error_alert(str(e), context="run_trading_job")
        except Exception:
            pass
    finally:
        await kalshi.close()
        await comparator.close()

    if results.total_capital_used > 0:
        results.capital_efficiency = min(results.total_capital_used / 1000.0, 1.0)

    logger.info(
        "━━━ CYCLE DONE (%s) | trades=%d (arb=%d ai=%d skipped=%d) | "
        "capital=$%.2f ━━━",
        mode_label,
        results.total_positions,
        results.arb_trades,
        results.ai_trades,
        results.skipped,
        results.total_capital_used,
    )
    return results
