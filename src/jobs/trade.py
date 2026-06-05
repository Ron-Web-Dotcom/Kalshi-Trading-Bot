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
    open_count   = len(open_positions_rows)
    open_tickers = {p["ticker"] for p in open_positions_rows}
    # Block same question across platforms — but only for long-duration positions (open > 1 hour)
    # Short-duration trades (5min/10min/hourly) resolve fast so re-entry on a new cycle is fine
    from datetime import datetime as _now_dt, timezone as _now_tz, timedelta as _now_td
    _now = _now_dt.now(_now_tz.utc)
    open_titles = set()
    for p in open_positions_rows:
        if not p.get("title"):
            continue
        try:
            opened = _now_dt.fromisoformat((p.get("opened_at") or "").replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=_now_tz.utc)
            if (_now - opened) > _now_td(hours=1):
                open_titles.add(p["title"].strip().lower())
        except Exception:
            open_titles.add(p["title"].strip().lower())
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
                        market_title=(market.get("title") or ticker) if market else ticker,
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

        # ── 5. Fetch Polymarket candidates + store in DB (always — needed for position tracking) ──
        poly_markets = []
        if poly_enabled:
            try:
                raw_poly = await poly_client.get_markets(limit=1000)
                now_ts   = __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat()

                # Persist Polymarket markets to DB so tracker can read live prices
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
                    and m.get("ticker") not in open_tickers
                    and (m.get("title") or "").strip().lower() not in open_titles
                ]
                logger.info("Polymarket: %d markets stored, %d tradeable",
                            len(raw_poly), len(poly_markets))
            except Exception as pe:
                logger.warning("Polymarket market load failed: %s", pe)
                poly_markets = []

        # ── 6. Daily trade gate — sit out TRADING if limit hit, but keep scanning ─
        from src.utils.eastern_time import now_et as _now_et_trade
        today = _now_et_trade().date().isoformat()
        paper_flag = 0 if live_mode else 1
        trades_today_row = await db.fetchone(
            "SELECT COUNT(*) AS n FROM trade_logs WHERE paper_trade=? AND executed_at >= ?",
            (paper_flag, today + "T00:00:00",)
        )
        trades_today  = (trades_today_row or {}).get("n", 0)
        max_per_day   = settings.trading.max_trades_per_day
        trade_gate_on = (trades_today >= max_per_day and trades_this_cycle == 0)

        if trade_gate_on:
            logger.info(
                "Daily trade limit reached (%d/%d) — SCANNING continues, trading paused",
                trades_today, max_per_day,
            )

        # ── 7. Best-opportunity hunt across BOTH platforms ────────────────────
        from src.strategy.opportunity import OpportunityHunter
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        arb_tickers = {s["ticker"] for s in all_signals}
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

        def _tradeable_price(m):
            """Return the best available price for a market — ask first, last_price fallback."""
            ask = m.get("yes_ask", 0) or 0
            return ask if ask > 0 else (m.get("last_price", 0) or 0)

        def _already_open(m):
            if m.get("ticker") in open_tickers:
                return True
            t = (m.get("title") or "").strip().lower()
            return bool(t and t in open_titles)

        # ── Live / in-play market pool (highest priority) ─────────────────────
        # A market is only LIVE if is_event_live_now() confirms an actual
        # real-world event is happening RIGHT NOW (game in progress, match
        # underway, press conference live, etc.).
        #
        # Time-based "closing soon" is NOT sufficient to call something live —
        # "Will Rihanna release an album?" closing in 0h is not a live event.
        # Those markets go into the regular scan as EXPIRING candidates instead.
        from src.data.live_event_detector import is_event_live_now

        # Keywords that suggest a real live event could be happening
        _LIVE_EVENT_KEYWORDS = {
            # Sports
            "nfl", "nba", "mlb", "nhl", "ufc", "mma", "soccer", "football",
            "basketball", "baseball", "hockey", "tennis", "golf", "f1",
            "formula", "nascar", "super bowl", "world series", "stanley cup",
            "playoffs", "finals", "championship", "tournament", "match",
            "game", "fight", "bout", "race", "open", "wimbledon",
            # Live events
            "debate", "speech", "conference", "summit", "hearing", "trial",
            "vote", "election", "inauguration", "press",
            # Weather events that evolve in real-time
            "hurricane", "tornado", "storm",
        }

        def _could_be_live(title: str) -> bool:
            """Return True if the title suggests a real-time event might be live."""
            t = title.lower()
            return any(kw in t for kw in _LIVE_EVENT_KEYWORDS)

        expiring_kalshi_raw = []  # closing soon but NOT confirmed live
        expiring_poly_raw   = []  # same for Poly
        live_kalshi_raw     = []  # confirmed live by is_event_live_now()
        live_poly_raw       = []  # confirmed live by is_event_live_now()

        # Fetch all markets closing within time windows for event checking
        try:
            kalshi_6h_raw  = await kalshi.get_live_markets(max_hours=6.0,  max_markets=500)
            kalshi_24h_raw = await kalshi.get_live_markets(max_hours=24.0, max_markets=500)
        except Exception as _le:
            logger.debug("Live Kalshi fetch skipped: %s", _le)
            kalshi_6h_raw  = []
            kalshi_24h_raw = []

        poly_time_raw = []
        if poly_enabled:
            try:
                poly_time_raw = await poly_client.get_live_markets(max_hours=48.0, max_markets=500)
            except Exception as _le:
                logger.debug("Live Polymarket fetch skipped: %s", _le)

        # Run is_event_live_now() on all candidates that COULD be live events
        all_kalshi_candidates = {m.get("ticker"): m for m in kalshi_24h_raw}
        kalshi_live_check = [
            (m, is_event_live_now(m.get("title", "")))
            for m in all_kalshi_candidates.values()
            if _could_be_live(m.get("title", ""))
        ]
        poly_live_check = [
            (m, is_event_live_now(m.get("title", "")))
            for m in poly_time_raw
            if _could_be_live(m.get("title", ""))
        ]

        # Gather all live checks in parallel
        try:
            if kalshi_live_check or poly_live_check:
                all_tasks   = [t for _, t in kalshi_live_check] + [t for _, t in poly_live_check]
                all_markets = [m for m, _ in kalshi_live_check] + [m for m, _ in poly_live_check]
                all_results = await asyncio.gather(*all_tasks, return_exceptions=True)
                k_len = len(kalshi_live_check)
                for m, result in zip(all_markets[:k_len], all_results[:k_len]):
                    if result is True:
                        m["_live_confirmed"] = True
                        live_kalshi_raw.append(m)
                    else:
                        expiring_kalshi_raw.append(m)
                for m, result in zip(all_markets[k_len:], all_results[k_len:]):
                    if result is True:
                        m["_live_confirmed"] = True
                        live_poly_raw.append(m)
                    else:
                        expiring_poly_raw.append(m)
        except Exception as _le:
            logger.debug("Live event detector failed: %s", _le)

        # Markets without live-event keywords → expiring only (no live check)
        for m in all_kalshi_candidates.values():
            if not _could_be_live(m.get("title", "")) and m.get("ticker") not in {x.get("ticker") for x in live_kalshi_raw}:
                expiring_kalshi_raw.append(m)
        for m in poly_time_raw:
            if not _could_be_live(m.get("title", "")) and m.get("ticker") not in {x.get("ticker") for x in live_poly_raw}:
                expiring_poly_raw.append(m)

        live_kalshi = [
            m for m in live_kalshi_raw
            if not _already_open(m)
            and m.get("ticker") not in arb_tickers
            and 2 < _tradeable_price(m) < 98
            and (m.get("title") or "")
        ]
        live_poly = [
            m for m in live_poly_raw
            if not _already_open(m)
            and m.get("ticker") not in open_tickers
            and m.get("yes_ask", 0) > 1
            and (m.get("title") or "")
        ]

        # Log confirmed live events separately from expiring markets
        if live_kalshi or live_poly:
            logger.info(
                "── LIVE NOW (confirmed): %d Kalshi + %d Polymarket ─",
                len(live_kalshi), len(live_poly),
            )
            for lm in live_kalshi[:5]:
                logger.info(
                    "  [LIVE-K] %s | %.0fh left | %.0f¢",
                    (lm.get("title") or lm.get("ticker") or "?")[:60],
                    lm.get("hours_to_close", 0), _tradeable_price(lm),
                )
            for lm in live_poly[:5]:
                logger.info(
                    "  [LIVE-P] %s | %.0fh left | %.0f¢",
                    (lm.get("title") or lm.get("ticker") or "?")[:60],
                    lm.get("hours_to_close", 0), lm.get("yes_ask", 0),
                )
        else:
            logger.info("── No confirmed live events this cycle ─")

        expiring_n = len(expiring_kalshi_raw) + len(expiring_poly_raw)
        if expiring_n:
            logger.info(
                "── EXPIRING (closing soon, not live): %d markets → added to regular scan",
                expiring_n,
            )

        # Hoist sizing constants so both live pass and regular pass can use them
        min_size = settings.trading.min_trade_size_dollars
        max_size = settings.trading.max_trade_size_dollars

        # ── LIVE TRADING PASS: top 3 confident picks from in-play markets ───────
        all_live = live_kalshi + live_poly
        if all_live:
            live_hunter = OpportunityHunter(db=db)
            top_live = await live_hunter.find_top_live(
                live_markets   = all_live,
                arb_signals    = all_signals,
                min_confidence = settings.trading.min_ai_confidence,
                top_n          = 3,
                ai_eval_n      = min(6, len(all_live)),
            )

            if top_live:
                # Execute each live trade; collect only the ones that actually went through
                executed_live_trades = []
                for r in top_live:
                    if trades_this_cycle >= max_trades + 3:  # live trades get up to 3 extra slots
                        break
                    m        = r["market"]
                    decision = r["decision"]
                    live_side  = r["side"]
                    live_price = r["price_cents"]
                    live_tick  = m.get("ticker", "")
                    live_net_ev = decision.get("net_ev")

                    # Skip if already open
                    if _already_open(m):
                        continue

                    # Profit gate (relaxed for live)
                    live_conf = float(decision.get("confidence", 0))
                    live_base = scaler.current_size
                    live_mult = (1.5 if live_conf >= 90 else
                                 1.0 if live_conf >= 80 else
                                 0.5 if live_conf >= 70 else 0.25)
                    live_size = round(max(min_size, min(live_base * live_mult, max_size)), 2)
                    live_contracts = (live_size / (live_price / 100)) if live_price > 0 else 0
                    live_exp_profit = live_contracts * (live_net_ev / 100) if live_net_ev else None
                    live_roi = (live_exp_profit / live_size * 100) if (live_exp_profit and live_size) else None
                    live_min_roi = settings.trading.min_profit_roi_pct * 0.4
                    live_min_abs = settings.trading.min_profit_abs_usd * 0.4

                    if live_exp_profit is None or live_exp_profit < live_min_abs or (live_roi or 0) < live_min_roi:
                        logger.info(
                            "LIVE SKIP %s — profit gate: $%.2f (%.1f%% ROI) < min $%.2f / %.1f%%",
                            live_tick, live_exp_profit or 0, live_roi or 0,
                            live_min_abs, live_min_roi,
                        )
                        results.skipped += 1
                        daily_stats.record_skip("live_profit_gate")
                        continue

                    live_allowed, live_reason = risk.check_trade(
                        live_tick, scaler.current_size,
                        current_positions=[], portfolio_value=portfolio_val,
                    )
                    if not live_allowed:
                        logger.info("LIVE SKIP %s — risk gate: %s", live_tick, live_reason)
                        results.skipped += 1
                        daily_stats.record_skip(f"live_risk_gate:{live_reason}")
                        continue

                    live_platform = r["platform"]
                    active_trader = kalshi_trader if live_platform == "kalshi" else poly_trader
                    logger.info(
                        "LIVE TRADE: [%s] %s BUY %s @ %.0f¢ | conf=%d%% EV=%.1f¢ size=$%.2f",
                        live_platform.upper(), live_tick, live_side.upper(), live_price,
                        live_conf, live_net_ev or 0, live_size,
                    )
                    rec = await active_trader.execute(
                        ticker=live_tick,
                        action=decision["action"],
                        side=live_side,
                        price_cents=live_price,
                        ai_confidence=live_conf,
                        ai_reasoning=decision["reasoning"],
                        signal_source="live_scan",
                        net_ev=live_net_ev,
                        market_title=m.get("title", ""),
                        **({"poly_token_id": m.get("_yes_token") if live_side == "yes" else m.get("_no_token")}
                           if live_platform == "polymarket" else {}),
                    )
                    if rec:
                        trades_this_cycle += 1
                        results.total_positions += 1
                        results.total_capital_used += rec.get("total_cost", 0)
                        results.ai_trades += 1
                        open_tickers.add(live_tick)  # prevent duplicate in same cycle
                        await auditor.log(
                            db, "TRADE_PLACED", ticker=live_tick, platform=live_platform,
                            side=live_side, price_cents=live_price,
                            size_usd=rec.get("total_cost", 0),
                            confidence=live_conf, net_ev=live_net_ev,
                            reason=decision.get("reasoning", "")[:200],
                        )
                        executed_live_trades.append({
                            "ticker":         live_tick,
                            "title":          m.get("title", ""),
                            "platform":       live_platform,
                            "side":           live_side,
                            "price_cents":    live_price,
                            "confidence":     live_conf,
                            "net_ev":         live_net_ev or 0,
                            "size_usd":       rec.get("total_cost", live_size),
                            "contracts":      rec.get("contracts", 0),
                            "reasoning":      decision.get("reasoning", ""),
                            "hours_to_close": m.get("hours_to_close", 0),
                        })

                # live_trades_alert is owned by live_market_manager — skip here to avoid duplicate alerts

        # Long-term pool: higher-volume markets (any close time)
        long_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and not _already_open(m)
            and 2 < _tradeable_price(m) < 98
            and m.get("volume", 0) >= min_vol
            and (m.get("title") or "")
        ]

        # Short-duration pool: closes within 24h — any volume, for 1min/5min/1hr/daily markets
        short_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and not _already_open(m)
            and 2 < _tradeable_price(m) < 98
            and m.get("ticker") not in {x.get("ticker") for x in long_term}
            and _closes_within(m, 24)
            and (m.get("title") or "")
        ]

        # ── Category-wide sweep: scan ALL categories + sub-categories ─────────
        # Runs in background and merges into candidate pools for broader coverage.
        # This is what lets the bot find "cheeky bids" across every market type.
        try:
            from src.data.category_scanner import CategoryScanner
            cat_scanner = CategoryScanner(db=db)
            cat_markets = await cat_scanner.scan_all_categories(
                max_per_tag=50,
                max_total=999999,
                include_bulk=True,
            )
            # Separate Kalshi and Polymarket results from category scan
            cat_kalshi = [
                m for m in cat_markets
                if m.get("platform", "kalshi") != "polymarket"
                and m.get("ticker") not in arb_tickers
                and not _already_open(m)
                and 2 < _tradeable_price(m) < 98
            ]
            cat_poly = [
                m for m in cat_markets
                if m.get("platform") == "polymarket"
                and not _already_open(m)
                and m.get("yes_ask", 0) > 1
            ]
            # Merge category-scanned markets in, deduplicating by ticker
            existing_kalshi_tickers = {m.get("ticker") for m in long_term + short_term + live_kalshi}
            existing_poly_tickers   = {m.get("ticker") for m in poly_markets}
            cat_kalshi_new = [m for m in cat_kalshi if m.get("ticker") not in existing_kalshi_tickers]
            cat_poly_new   = [m for m in cat_poly   if m.get("ticker") not in existing_poly_tickers]
            # Add to pools — category markets go after existing candidates (which came from DB volume-sort)
            long_term    = long_term    + cat_kalshi_new
            poly_markets = poly_markets + cat_poly_new
            logger.info(
                "Category sweep added: +%d Kalshi candidates, +%d Polymarket candidates",
                len(cat_kalshi_new), len(cat_poly_new),
            )
        except Exception as _cat_err:
            logger.debug("Category sweep error (non-fatal): %s", _cat_err)

        # Expiring markets (closing soon but NOT confirmed live) → regular scan pool
        # They still get AI evaluated, just without the live-trade priority boost
        existing_all_tickers = {m.get("ticker") for m in long_term + short_term + live_kalshi + poly_markets}
        for m in expiring_kalshi_raw:
            if (m.get("ticker") not in existing_all_tickers
                    and not _already_open(m)
                    and 2 < _tradeable_price(m) < 98):
                long_term.append(m)
                existing_all_tickers.add(m.get("ticker"))
        for m in expiring_poly_raw:
            if (m.get("ticker") not in existing_all_tickers
                    and not _already_open(m)
                    and m.get("yes_ask", 0) > 1):
                poly_markets.append(m)
                existing_all_tickers.add(m.get("ticker"))

        # Live markets go FIRST — they're time-sensitive and get AI evaluated before regular markets
        live_kalshi_tickers = {m.get("ticker") for m in live_kalshi}
        long_term  = [m for m in long_term  if m.get("ticker") not in live_kalshi_tickers]
        short_term = [m for m in short_term if m.get("ticker") not in live_kalshi_tickers]
        kalshi_candidates = live_kalshi + long_term + short_term

        # Merge live Polymarket into poly_markets (deduplicate)
        poly_tickers_existing = {m.get("ticker") for m in poly_markets}
        live_poly_new = [m for m in live_poly if m.get("ticker") not in poly_tickers_existing]
        poly_markets = live_poly_new + poly_markets   # live first

        logger.info(
            "── Best-Opportunity Hunt: %d Kalshi (%d live + %d long + %d short) + %d Polymarket ─",
            len(kalshi_candidates), len(live_kalshi), len(long_term),
            len(short_term), len(poly_markets),
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

        # Live markets get one extra trade slot per cycle — they're time-sensitive
        live_bonus = 1 if (best and best.get("market", {}).get("is_live")) else 0
        if best and not trade_gate_on and trades_this_cycle < max_trades + live_bonus:
            market   = best["market"]
            decision = best["decision"]
            poly_comp= best.get("poly_comp")
            side     = best["side"]
            price    = best["price_cents"]
            ticker   = market.get("ticker", "")
            net_ev   = decision.get("net_ev")

            # Confidence-tiered sizing:
            #   60–69%  → minimum size (25% of base) — small exploratory bet
            #   70–79%  → half size  (50% of base) — medium conviction
            #   80–89%  → full size  (100% of base) — high conviction
            #   90–100% → max size   (150% of base, capped) — very high conviction
            confidence = float(decision.get("confidence", 0))
            base       = scaler.current_size
            if confidence >= 90:
                size_multiplier = 1.5
                size_tier       = "MAX (90%+ conf)"
            elif confidence >= 80:
                size_multiplier = 1.0
                size_tier       = "FULL (80–89% conf)"
            elif confidence >= 70:
                size_multiplier = 0.5
                size_tier       = "HALF (70–79% conf)"
            else:
                size_multiplier = 0.25
                size_tier       = "MIN (60–69% conf)"
            planned_size_usd = round(max(min_size, min(base * size_multiplier, max_size)), 2)
            is_live_market   = bool(market.get("is_live"))
            logger.info("Trade size: $%.2f [%s]%s", planned_size_usd, size_tier,
                        " [LIVE]" if is_live_market else "")
            contracts_est  = (planned_size_usd / (price / 100)) if price > 0 else 0
            exp_profit_usd = contracts_est * (net_ev / 100) if net_ev is not None else None
            roi_pct        = (exp_profit_usd / planned_size_usd * 100) if (exp_profit_usd and planned_size_usd) else None

            # Use settings directly — no hardcoded floors overriding config
            min_roi = settings.trading.min_profit_roi_pct
            min_abs = settings.trading.min_profit_abs_usd
            if is_live_market:
                # Live markets resolve fast — relax gate by 60%
                min_roi *= 0.4
                min_abs *= 0.4

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
                            f" | Poly_YES={poly_comp.get('poly_yes', 0):.0f}¢"
                            f" Poly_NO={poly_comp.get('poly_no', 0):.0f}¢"
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
                _s.polymarket.api_secret, _s.ai.openai_api_key,
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
