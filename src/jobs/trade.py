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


async def run_trading_job(db=None, risk=None, scaler=None, arb_det=None) -> TradingResults:
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

    results = TradingResults()

    # ── Kill switch check (must be FIRST) ──────────────────────────────────
    from src.utils.kill_switch import is_active as kill_switch_active
    from src.utils.audit_log import auditor
    if kill_switch_active():
        logger.warning("KILL SWITCH ACTIVE — trading halted")
        await auditor.log(db, "KILL_SWITCH", reason="kill switch active")
        return results

    live_mode     = settings.trading.live_trading_enabled
    poly_enabled  = settings.polymarket.enabled
    max_trades    = settings.trading.max_trades_per_cycle
    max_scan      = settings.trading.max_markets_to_scan
    min_vol       = settings.trading.min_market_volume
    portfolio_val = settings.trading.portfolio_value

    kalshi            = KalshiClient()
    poly_client       = PolymarketTradingClient()
    fetcher           = MarketDataFetcher(kalshi, db)
    comparator        = ExternalMarketComparator(db)
    # Use singletons passed from TradingBot when available (preserves cooldown/scale state)
    arb               = arb_det   if arb_det  is not None else ArbitrageDetector()
    risk              = risk       if risk     is not None else RiskManager(db)
    scaler            = scaler     if scaler   is not None else AutoScaler()
    discord           = DiscordAlerter()

    # Fetch live balance so Kelly and risk checks use real portfolio size (A2)
    if live_mode:
        try:
            bal = await kalshi.get_balance()
            live_balance = (bal.get("balance") or 0) / 100  # cents → dollars
            if live_balance > 0:
                portfolio_val = live_balance
                settings.trading.portfolio_value = portfolio_val
                logger.info("Live portfolio: $%.2f", portfolio_val)
        except Exception as _be:
            logger.warning("Could not fetch live balance — using config $%.2f: %s", portfolio_val, _be)
    results           = TradingResults()
    trades_this_cycle = 0

    # ── Daily loss lockout check ──────────────────────────────────────────
    locked, lockout_reason = await risk.check_daily_loss_lockout(db)
    if locked:
        logger.warning("RISK LOCKOUT: %s", lockout_reason)
        await discord.error_alert(lockout_reason, context="daily_loss_lockout")
        await auditor.log(db, "LOCKOUT", reason=lockout_reason)
        return results

    # ── Open positions: log them + check cap ─────────────────────────────
    open_positions_rows = await db.fetchall(
        "SELECT ticker, side, contracts, avg_price, current_price, pnl, platform, title "
        "FROM positions WHERE status='open'"
    )
    open_count = len(open_positions_rows)
    if open_count > 0:
        logger.info("── Open Positions (%d) ──────────────────────────────────────────", open_count)
        for _p in open_positions_rows:
            _pnl = _p.get("pnl") or 0
            _cur = _p.get("current_price") or _p.get("avg_price") or 0
            _ent = _p.get("avg_price") or 0
            _pct = ((_cur - _ent) / _ent * 100) if _ent else 0
            _lbl = (_p.get("title") or _p.get("ticker") or "?")[:40]
            logger.info(
                "  %-40s  %s  %dx  entry=%.0f¢  now=%.0f¢  (%+.1f%%)  PnL=%+.2f",
                _lbl, (_p.get("side") or "?").upper(), _p.get("contracts", 0),
                _ent, _cur, _pct, _pnl,
            )

    if open_count >= settings.trading.max_open_positions:
        logger.info(
            "Max open positions (%d) reached — skipping new trade scan this cycle",
            open_count,
        )
        return results

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
        from src.utils.daily_stats import stats as daily_stats
        daily_stats.record_markets_scanned(len(markets))

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
                daily_stats.record_skip("not_in_cached_markets")
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
                        daily_stats.record_skip(f"risk_gate:{reason}")
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
                        daily_stats.record_trade(
                            ticker=ticker, side=side, confidence=99.0,
                            net_ev=net, score=1.0,
                            reasoning=f"Internal arb: YES+NO={yes_p+no_p:.0f}¢ net={net:.1f}¢",
                        )
                        await auditor.log(
                            db, "TRADE_PLACED", ticker=ticker, side=side,
                            price_cents=price, size_usd=rec.get("total_cost", 0),
                            confidence=99.0, net_ev=net,
                            reason=f"Internal arb: YES+NO={yes_p+no_p:.0f}¢ net={net:.1f}¢",
                        )
                        if discord.cfg.alert_on_signal:
                            await discord.arb_signal(
                                ticker=ticker, signal_type="internal_arb",
                                gross_edge=sig["gross_edge_cents"],
                                net_edge=net,
                                kalshi_price=yes_p,
                                poly_price=no_p,
                                market_title=market.get("title", "") or market.get("question", ""),
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
                    daily_stats.record_skip("price_out_of_range")
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
                    daily_stats.record_skip(f"risk_gate:{reason}")
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
                    daily_stats.record_trade(
                        ticker=ticker, side=side, confidence=95.0,
                        net_ev=net, score=0.9,
                        reasoning=(
                            f"Cross-market arb: Kalshi={sig['kalshi_price']:.0f}¢ "
                            f"vs Poly={sig['poly_price']:.0f}¢ net={net:.1f}¢"
                        ),
                    )
                    await auditor.log(
                        db, "TRADE_PLACED", ticker=ticker, side=side,
                        price_cents=price, size_usd=rec.get("total_cost", 0),
                        confidence=95.0, net_ev=net,
                        reason=f"Cross-market arb: Kalshi={sig['kalshi_price']:.0f}¢ vs Poly={sig['poly_price']:.0f}¢",
                    )
                    if discord.cfg.alert_on_signal:
                        await discord.arb_signal(
                            ticker=ticker, signal_type="cross_market_arb",
                            gross_edge=sig["gross_edge_cents"],
                            net_edge=net,
                            side=side,
                            kalshi_price=sig["kalshi_price"],
                            poly_price=sig["poly_price"],
                            market_title=market.get("title", "") or market.get("question", ""),
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

        # ── 6. Fetch Polymarket candidates + store in DB ──────────────────────────
        poly_markets = []
        if poly_enabled:
            try:
                raw_poly = await poly_client.get_markets(limit=1000)
                now_ts   = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat()

                # Persist Polymarket markets to DB so heartbeat can count them
                for pm in raw_poly:
                    try:
                        await db.execute("""
                            INSERT OR REPLACE INTO markets
                            (ticker, title, category, status, yes_bid, yes_ask,
                             no_bid, no_ask, volume, open_interest, close_time,
                             last_price, fetched_at, platform)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            pm["ticker"], pm.get("title","")[:200],
                            pm.get("category",""), "open",
                            pm.get("yes_bid",0), pm.get("yes_ask",0),
                            pm.get("no_bid",0),  pm.get("no_ask",0),
                            pm.get("volume",0),  0,
                            pm.get("close_time",""), pm.get("yes_ask",0),
                            now_ts, "polymarket",
                        ))
                    except Exception:
                        pass

                poly_markets = [
                    m for m in raw_poly
                    if m.get("yes_ask", 0) > 1
                ][:max_scan]
                logger.info("Polymarket: %d markets stored, %d tradeable",
                            len(raw_poly), len(poly_markets))
            except Exception as pe:
                logger.warning("Polymarket market load failed: %s", pe)
                poly_markets = []

        # ── 7. Best-opportunity hunt across BOTH platforms ────────────────────
        from src.strategy.opportunity import OpportunityHunter
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        arb_tickers = {s["ticker"] for s in all_signals}
        open_positions = await db.fetchall("SELECT ticker FROM positions WHERE status='open'")
        open_tickers = {p["ticker"] for p in (open_positions or [])}
        now_utc = _dt.now(_tz.utc)

        def _closes_within(m, hours):
            ct = m.get("close_time", "")
            if not ct:
                return False
            try:
                close_dt = _dt.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=_tz.utc)
                return 0 < (close_dt - now_utc).total_seconds() / 3600 <= hours
            except Exception:
                return False

        # Long-term pool: higher-volume markets (any close time)
        long_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and m.get("ticker") not in open_tickers
            and 2 < m.get("yes_ask", 0) < 98
            and m.get("volume", 0) >= min_vol
        ][:max_scan]

        # Short-duration pool: closes within 24h — lower volume threshold so
        # 1min/5min/1hr/daily markets are included regardless of cumulative volume
        short_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and m.get("ticker") not in open_tickers
            and 2 < m.get("yes_ask", 0) < 98
            and m.get("ticker") not in {x.get("ticker") for x in long_term}
            and _closes_within(m, 24)
        ][:max_scan // 2]

        kalshi_candidates = long_term + short_term

        logger.info(
            "── Best-Opportunity Hunt: %d Kalshi (%d long-term + %d short-duration) + %d Polymarket ─",
            len(kalshi_candidates), len(long_term), len(short_term), len(poly_markets),
        )

        hunter = OpportunityHunter(db=db)
        best   = await hunter.find_best(
            markets      = kalshi_candidates,
            arb_signals  = all_signals,
            poly_comps   = ext_comps,
            min_score    = 0.01,   # paper mode: low bar to see the bot in action
            poly_markets = poly_markets,
        )

        if not best:
            # Log reason; no_opportunity() Discord method is disabled (returns immediately)
            # — covered by the hourly heartbeat instead. Also suppress if positions are open.
            logger.info(
                "No best opportunity found across %d Kalshi + %d Polymarket candidates "
                "(open_positions=%d)",
                len(kalshi_candidates), len(poly_markets), open_count,
            )

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
                skip_reason = (
                    f"profit gate: {f'${exp_profit_usd:.2f}' if exp_profit_usd is not None else 'EV=null'} "
                    f"({roi_pct or 0:.1f}% ROI) < min ${min_abs:.2f} / {min_roi:.1f}%"
                )
                logger.info("Best opportunity SKIPPED — %s", skip_reason)
                results.skipped += 1
                daily_stats.record_skip("profit_gate")
                await auditor.log(
                    db, "TRADE_SKIPPED", ticker=ticker, side=side,
                    price_cents=price, size_usd=planned_size_usd,
                    confidence=decision.get("confidence", 0), net_ev=net_ev,
                    reason=skip_reason, result="SKIPPED",
                )
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
                    daily_stats.record_skip(f"risk_gate:{reason}")
                    await auditor.log(
                        db, "TRADE_SKIPPED", ticker=ticker, side=side,
                        price_cents=price, size_usd=planned_size_usd,
                        confidence=decision.get("confidence", 0), net_ev=net_ev,
                        reason=f"risk_gate:{reason}", result="SKIPPED",
                    )
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

                    # Route to the correct platform's trader (single Discord alert comes from execute())
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
                        true_prob=decision.get("true_prob"),
                        market_title=market.get("title", ""),
                        **({"poly_token_id": market.get("_yes_token") if side == "yes" else market.get("_no_token")}
                           if platform == "polymarket" else {}),
                    )
                    if rec:
                        trades_this_cycle += 1
                        results.total_positions += 1
                        results.total_capital_used += rec.get("total_cost", 0)
                        results.ai_trades += 1
                        await auditor.log(
                            db, "TRADE_PLACED", ticker=ticker, platform=platform,
                            side=side, price_cents=price,
                            size_usd=rec.get("total_cost", 0),
                            confidence=decision.get("confidence", 0),
                            net_ev=net_ev,
                            reason=decision.get("reasoning", "")[:200],
                        )

    except Exception as e:
        logger.error("Trade job crashed: %s", e, exc_info=True)
        try:
            from src.utils.daily_stats import stats as daily_stats
            daily_stats.record_error(str(e)[:200])
        except Exception:
            pass
        try:
            from src.config.settings import settings as _s
            _err = str(e)[:500]
            for _secret in filter(None, [
                _s.kalshi.api_key_id, _s.kalshi.api_key,
                _s.polymarket.api_secret, _s.ai.anthropic_api_key,
            ]):
                _err = _err.replace(_secret, "[REDACTED]")
            await discord.error_alert(_err, context="run_trading_job")
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
