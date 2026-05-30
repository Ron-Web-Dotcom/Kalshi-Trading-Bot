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
    from src.execution.poly_paper_trader import PolyPaperTrader
    from src.risk.manager import RiskManager
    from src.risk.scaling import AutoScaler
    from src.alerts.discord import DiscordAlerter
    from src.clients.kalshi_client import KalshiClient
    from src.clients.polymarket_client import PolymarketTradingClient
    from src.utils.database import DatabaseManager

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    live_mode     = settings.trading.live_trading_enabled
    poly_enabled  = settings.polymarket.enabled
    max_trades    = settings.trading.max_trades_per_cycle
    max_scan      = settings.trading.max_markets_to_scan
    min_vol       = settings.trading.min_market_volume
    portfolio_val = settings.trading.portfolio_value

    kalshi     = KalshiClient()
    poly_client = PolymarketTradingClient()
    fetcher    = MarketDataFetcher(kalshi, db)
    comparator = ExternalMarketComparator(db)
    arb        = ArbitrageDetector()
    risk       = RiskManager(db)
    scaler     = AutoScaler()
    discord    = DiscordAlerter()
    results    = TradingResults()
    kalshi      = KalshiClient()
    poly_client = PolymarketTradingClient()
    fetcher     = MarketDataFetcher(kalshi, db)
    comparator  = ExternalMarketComparator(db)
    arb         = ArbitrageDetector()
    risk        = RiskManager(db)
    scaler      = AutoScaler()
    discord     = DiscordAlerter()
    results     = TradingResults()
    trades_this_cycle = 0

    mode_label = "LIVE" if live_mode else "PAPER"
    poly_label = "+POLY" if poly_enabled else ""

    # Build traders (one per platform)
    if live_mode:
        from src.execution.live_trader import LiveTrader
        kalshi_trader = LiveTrader(kalshi=kalshi, db=db, discord=discord,
                                   scaler=scaler, risk=risk)
    else:
        kalshi_trader = PaperTrader(db=db, discord=discord, scaler=scaler, risk=risk)

    poly_trader = PolyPaperTrader(db=db, discord=discord, scaler=scaler, risk=risk)

    logger.info("━━━ TRADING CYCLE START (%s%s) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━", mode_label, poly_label)

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
                    rec = await kalshi_trader.execute(
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
                                kalshi_price=yes_p,
                                poly_price=no_p,
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

                rec = await kalshi_trader.execute(
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

        # ── 5. Daily trade gate — sit out if already traded today ────────────────
        from datetime import date as _date
        today = _date.today().isoformat()
        paper_flag = 0 if live_mode else 1   # live trades recorded as paper_trade=0
        trades_today_row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM trade_logs WHERE paper_trade=? AND executed_at >= ?",
            (paper_flag, today + "T00:00:00",)
        )
        trades_today = (trades_today_row or {}).get("n", 0)
        max_per_day  = settings.trading.max_trades_per_day

        if trades_today >= max_per_day and trades_this_cycle == 0:
            logger.info(
                "Daily trade limit reached (%d/%d today) — sitting out this cycle",
                trades_today, max_per_day,
            )
            return results

        # ── 6. Fetch Polymarket candidates ────────────────────────────────────────
        poly_markets = []
        if poly_enabled:
            try:
                poly_markets = await poly_client.get_markets(limit=200)
                # Filter to reasonable volume and price range
                poly_markets = [
                    m for m in poly_markets
                    if m.get("volume", 0) >= min_vol
                    and 5 < m.get("yes_ask", 0) < 95
                ][:max_scan]
                logger.info("Polymarket: %d tradeable markets loaded", len(poly_markets))
            except Exception as pe:
                logger.warning("Polymarket market load failed: %s", pe)
                poly_markets = []

        # ── 7. Best-opportunity hunt across BOTH platforms ────────────────────
        from src.strategy.opportunity import OpportunityHunter

        arb_tickers = {s["ticker"] for s in all_signals}
        kalshi_candidates = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and 5 < m.get("yes_ask", 0) < 95
            and m.get("volume", 0) >= min_vol
        ][:max_scan]

        logger.info(
            "── Best-Opportunity Hunt: %d Kalshi + %d Polymarket candidates ─",
            len(kalshi_candidates), len(poly_markets),
        )

        hunter = OpportunityHunter(db=db)
        best   = await hunter.find_best(
            markets      = kalshi_candidates,
            arb_signals  = all_signals,
            poly_comps   = ext_comps,
            min_score    = 0.05,
            poly_markets = poly_markets,
        )

        if not best:
            try:
                await discord.no_opportunity(
                    markets_scanned=len(kalshi_candidates) + len(poly_markets), paper=not live_mode
                )
            except Exception:
                pass

        if best and trades_this_cycle < max_trades:
            market   = best["market"]
            decision = best["decision"]
            poly_comp= best.get("poly_comp")
            side     = best["side"]
            price    = best["price_cents"]
            ticker   = market.get("ticker", "")
            net_ev   = decision.get("net_ev")

            # Profit gate — None net_ev is treated as failing (not bypassing)
            planned_size_usd = scaler.current_size
            contracts_est    = (planned_size_usd / (price / 100)) if price > 0 else 0
            exp_profit_usd   = contracts_est * (net_ev / 100) if net_ev is not None else None
            roi_pct          = (exp_profit_usd / planned_size_usd * 100) if (exp_profit_usd and planned_size_usd) else None
            min_roi = settings.trading.min_profit_roi_pct
            min_abs = settings.trading.min_profit_abs_usd

            if exp_profit_usd is None or exp_profit_usd < min_abs or (roi_pct or 0) < min_roi:
                logger.info(
                    "Best opportunity SKIPPED — profit gate: %s (%.1f%% ROI) < min $%.2f / %.1f%%",
                    f"${exp_profit_usd:.2f}" if exp_profit_usd is not None else "EV=null",
                    roi_pct or 0, min_abs, min_roi,
                )
                results.skipped += 1
            else:
                daily_loss_db = await risk.get_daily_loss_from_db()
                allowed, reason = risk.check_trade(
                    ticker, scaler.current_size,
                    current_positions=[], portfolio_value=portfolio_val,
                    daily_loss_override=daily_loss_db,
                )
                if not allowed:
                    logger.info("Best opportunity BLOCKED by risk gate: %s", reason)
                    results.skipped += 1
                else:
                    poly_str = ""
                    if poly_comp:
                        poly_str = (
                            f" | Poly_YES={poly_comp['poly_yes']:.0f}¢"
                            f" Poly_NO={poly_comp['poly_no']:.0f}¢"
                        )
                    logger.info(
                        "TAKING BEST OPPORTUNITY: %s BUY %s @ %.0f¢ | "
                        "score=%.3f conf=%d%% EV=%.1f¢ exp_profit=$%.2f%s",
                        ticker, side.upper(), price,
                        best["score"], decision.get("confidence", 0),
                        net_ev or 0, exp_profit_usd or 0, poly_str,
                    )

                    # Discord: notify we found the best opportunity before placing
                    platform = best.get("platform", "kalshi")
                    try:
                        await discord.best_opportunity_found(
                            ticker=ticker,
                            side=side,
                            price_cents=price,
                            confidence=decision.get("confidence", 0),
                            net_ev=net_ev,
                            exp_profit=exp_profit_usd,
                            score=best["score"],
                            reasoning=decision.get("reasoning", ""),
                            poly_yes=poly_comp["poly_yes"] if poly_comp else None,
                            poly_no=poly_comp["poly_no"]  if poly_comp else None,
                            market_title=market.get("title", ""),
                            paper=not live_mode,
                            platform=platform,
                        )
                    except Exception:
                        pass

                    # Route to the correct platform's trader
                    platform = best.get("platform", "kalshi")
                    active_trader = kalshi_trader if platform == "kalshi" else poly_trader

                    rec = await active_trader.execute(
                        ticker=ticker,
                        action=decision["action"],
                        side=side,
                        price_cents=price,
                        ai_confidence=decision["confidence"],
                        ai_reasoning=decision["reasoning"],
                        signal_source=decision.get("model", "ai"),
                        net_ev=net_ev,
                        market_title=market.get("title", ""),
                        **({"poly_token_id": market.get("_yes_token") if side == "yes" else market.get("_no_token")}
                           if platform == "polymarket" else {}),
                        **({{"poly_token_id": market.get("_yes_token") if side == "yes" else market.get("_no_token")}}
                           if platform == "polymarket" else {{}}),
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
        await poly_client.close()
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
