"""Job: execute paper (or live) trades — full pipeline with detailed logging."""

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.utils.junk_filter import is_junk

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.jobs.trade")

_lockout_last_alerted: float = 0.0  # module-level — survives across cycles


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

    results           = TradingResults()
    trades_this_cycle = 0

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

    # ── Daily loss lockout check — BEFORE any API calls ───────────────────
    # No requests to Kalshi or Polymarket when locked out — saves API calls
    # and prevents rate limiting during the cooldown period.
    discord           = DiscordAlerter()
    risk              = risk if risk is not None else RiskManager(db)
    locked, lockout_reason = await risk.check_daily_loss_lockout(db)
    if locked:
        logger.warning("RISK LOCKOUT: %s", lockout_reason)
        # Only alert once per lockout — not every 60s cycle
        import time as _time
        global _lockout_last_alerted
        if _time.time() - _lockout_last_alerted > 1800:  # re-alert at most every 30 min
            try:
                await discord.send_message(
                    f"🛡️ **Cooling Down — Risk Protection Active**\n"
                    f"{lockout_reason}\n"
                    f"_Bot will resume scanning when the lockout clears. No bets placed during this period._"
                )
            except Exception:
                pass
            _lockout_last_alerted = _time.time()
        await auditor.log(db, "LOCKOUT", reason=lockout_reason)
        return results

    # ── Initialize API clients — only reached when NOT locked out ─────────
    kalshi            = KalshiClient()
    poly_client       = PolymarketTradingClient()
    fetcher           = MarketDataFetcher(kalshi, db)
    comparator        = ExternalMarketComparator(db)
    arb               = arb_det if arb_det is not None else ArbitrageDetector()
    scaler            = scaler  if scaler  is not None else AutoScaler()

    # Fetch live balance so Kelly and risk checks use real portfolio size
    if live_mode:
        try:
            bal = await kalshi.get_balance()
            live_balance = (bal.get("balance") or 0) / 100
            if live_balance > 0:
                portfolio_val = live_balance
                settings.trading.portfolio_value = portfolio_val
                logger.info("Live portfolio: $%.2f", portfolio_val)
        except Exception as _be:
            logger.warning("Could not fetch live balance — using config $%.2f: %s", portfolio_val, _be)

    # ── Open positions: log them + check cap ─────────────────────────────
    open_positions_rows = await db.fetchall(
        "SELECT ticker, side, contracts, avg_price, current_price, pnl, platform, title, opened_at "
        "FROM positions WHERE status='open'"
    )
    open_count   = len(open_positions_rows)
    open_tickers = {p["ticker"] for p in open_positions_rows}
    # Block same question across platforms — but only for long-duration positions (open > 1 hour)
    # Short-duration trades (5min/10min/hourly) resolve fast so re-entry on a new cycle is fine
    from datetime import datetime as _now_dt, timezone as _now_tz, timedelta as _now_td
    _now_et = _now_dt.now(_ET)
    open_titles = set()
    for p in open_positions_rows:
        if not p.get("title"):
            continue
        try:
            opened = _now_dt.fromisoformat((p.get("opened_at") or "").replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=_now_tz.utc).astimezone(_ET)
            if (_now_et - opened) > _now_td(hours=1):
                open_titles.add(p["title"].strip().lower())
        except Exception:
            pass
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
        try:
            from src.utils.daily_stats import stats as daily_stats
            daily_stats.record_skip("max_positions")
        except Exception:
            pass
        await kalshi.close()
        await poly_client.close()
        await comparator.close()
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

    # Fetch daily loss once at top level so arb + best-opp gates share the same value
    daily_loss_db = await risk.get_daily_loss_from_db()

    try:
        # ── 1. Load markets ───────────────────────────────────────────────────
        markets = await fetcher.get_cached_markets(min_volume=min_vol)
        if not markets:
            logger.warning("No markets in DB (run ingest first) — cycle skipped")
            # Still resolve expired positions even when market cache is empty
            try:
                await _resolve_expired_positions(db, live_mode, risk=risk)
            except Exception as _re:
                logger.debug("Real-time resolve error (empty cache path): %s", _re)
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

            if is_junk(market.get("title", "")):
                logger.info("SKIP arb %s — junk market title", ticker)
                results.skipped += 1
                daily_stats.record_skip("junk_filter")
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
                _arb_blocked = False
                for side, price in [("yes", yes_p), ("no", no_p)]:
                    allowed, reason = risk.check_trade(
                        ticker + f"_{side}", scaler.current_size,
                        current_positions=[], portfolio_value=portfolio_val,
                        daily_loss_override=daily_loss_db,
                        platform="kalshi",
                    )
                    if not allowed:
                        logger.info(
                            "SKIP internal-arb %s leg=%s | Reason: %s — aborting both legs",
                            ticker, side, reason,
                        )
                        results.skipped += 1
                        daily_stats.record_skip(f"risk_gate:{reason}")
                        _arb_blocked = True
                        break
                    if _arb_blocked:
                        break
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
                    daily_loss_override=daily_loss_db,
                    platform="kalshi",
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
                    market_title=market.get("title", "") or market.get("question", ""),
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

        # ── Date/time helpers — defined early, used by both poly fetch and pool filters ──
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        _ET_tz = _ZI("America/New_York")
        def _et_now_str() -> str:
            return _dt.now(_ET_tz).strftime("%Y-%m-%dT%H:%M:%S")
        arb_tickers = {s["ticker"] for s in all_signals}
        now_et = _dt.now(_ET)

        try:
            from src.utils.eastern_time import now_et as _get_et
            _et_now = _get_et()
        except Exception:
            import pytz as _pytz
            _et_now = _dt.now(_pytz.timezone("America/New_York"))
        _today_midnight_et   = _et_now.replace(hour=0, minute=0, second=0, microsecond=0)
        _tonight_et    = _today_midnight_et + _td(days=1)
        _tomorrow_end_et     = _today_midnight_et + _td(days=2)   # end of tomorrow ET
        _week_end_et         = _today_midnight_et + _td(days=7)

        def _closes_today(m):
            """True if market closes before tonight's midnight ET (today only)."""
            ct = m.get("close_time", "")
            if not ct:
                return False
            try:
                close_dt = _dt.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=_tz.utc).astimezone(_ET)
                return now_et < close_dt <= _tonight_et
            except Exception:
                return False

        def _closes_tomorrow(m):
            """True if market closes on tomorrow's ET calendar date."""
            ct = m.get("close_time", "")
            if not ct:
                return False
            try:
                close_dt = _dt.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=_tz.utc).astimezone(_ET)
                return _tonight_et < close_dt <= _tomorrow_end_et
            except Exception:
                return False

        def _closes_within_week(m):
            """True if market closes within 7 days from today's midnight."""
            ct = m.get("close_time", "")
            if not ct:
                return False
            try:
                close_dt = _dt.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=_tz.utc).astimezone(_ET)
                return now_et < close_dt <= _week_end_et
            except Exception:
                return False

        def _closes_within(m, hours):
            ct = m.get("close_time", "")
            if not ct:
                return False
            try:
                close_dt = _dt.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=_tz.utc).astimezone(_ET)
                return 0 < (close_dt - now_et).total_seconds() / 3600 <= hours
            except Exception:
                return False

        def _tradeable_price(m):
            ask = m.get("yes_ask", 0) or 0
            if ask > 0:
                return ask
            lp = m.get("last_price", 0) or 0
            if lp > 0:
                return lp
            bid = m.get("yes_bid", 0) or 0
            return bid

        def _already_open(m):
            if m.get("ticker") in open_tickers:
                return True
            t = (m.get("title") or "").strip().lower()
            if t and t in open_titles:
                return True
            # Block correlated exact-score / spread positions on the same event.
            # Strip score patterns and modifiers, then check if the remaining
            # "event words" overlap with any existing open position title.
            import re as _re
            _score_pat = _re.compile(
                r'\bexact score[:\s]*|\bspread[:\s]*|\bo/?u[:\s]*|\bover[/\s]under[:\s]*'
                r'|\b\d+[:\-]\d+\b|\(-?\d+\.?\d*\)'
                r'|\b(yes|no|will|the|be|on|at|in|a|an|is|to|of|and|or|for)\b'
                r'|[?!,.]',
                _re.IGNORECASE,
            )
            def _event_words(title: str):
                stripped = _score_pat.sub(' ', title.lower())
                words = {w for w in stripped.split() if len(w) > 2}
                return words
            new_words = _event_words(t)
            if len(new_words) >= 2:
                for existing_title in open_titles:
                    existing_words = _event_words(existing_title)
                    overlap = new_words & existing_words
                    if len(overlap) >= 2 and len(overlap) / max(len(new_words), 1) >= 0.5:
                        return True
            return False

        # ── 5. Fetch Polymarket candidates + store in DB (always — needed for position tracking) ──
        poly_markets = []
        if poly_enabled:
            try:
                raw_poly = await poly_client.get_markets(limit=500)
                now_ts   = _dt.now(_ET_tz).strftime("%Y-%m-%dT%H:%M:%S")

                # Persist Polymarket markets to DB so tracker can read live prices
                for pm in raw_poly:
                    try:
                        if is_junk(pm.get("title", "")):
                            continue
                        _pm_ticker = pm.get("ticker", "")
                        if not _pm_ticker:
                            continue
                        await db.execute("""
                            INSERT OR REPLACE INTO markets
                            (ticker, title, category, status, yes_bid, yes_ask,
                             no_bid, no_ask, volume, open_interest, close_time,
                             last_price, fetched_at, platform)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """, (
                            _pm_ticker, pm.get("title","")[:200],
                            pm.get("category",""), "open",
                            pm.get("yes_bid",0), pm.get("yes_ask",0),
                            pm.get("no_bid",0),  pm.get("no_ask",0),
                            pm.get("volume",0),  0,
                            pm.get("close_time",""), pm.get("yes_ask",0),
                            now_ts, "polymarket",
                        ))
                    except Exception:
                        pass

                _poly_base = [
                    m for m in raw_poly
                    if m.get("yes_ask", 0) > 1
                    and m.get("volume", 0) > 0             # hard reject zero-volume
                    and m.get("ticker") not in open_tickers
                    and (m.get("title") or "").strip().lower() not in open_titles
                    and m.get("close_time")
                    and not is_junk(m.get("title", ""))
                ]
                # Today only — live events with same-day results
                poly_markets = [m for m in _poly_base if _closes_today(m)]
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
        trade_gate_on = (trades_today >= max_per_day)

        if trade_gate_on:
            logger.info(
                "Daily trade limit reached (%d/%d) — SCANNING continues, trading paused",
                trades_today, max_per_day,
            )

        # ── 7. Best-opportunity hunt across BOTH platforms ────────────────────
        from src.strategy.opportunity import OpportunityHunter

        # ── Live / in-play market pool (highest priority) ─────────────────────
        # A market is only LIVE if is_event_live_now() confirms an actual
        # real-world event is happening RIGHT NOW (game in progress, match
        # underway, press conference live, etc.).
        #
        # Time-based "closing soon" is NOT sufficient to call something live —
        # "Will Rihanna release an album?" closing in 0h is not a live event.
        # Those markets go into the regular scan as EXPIRING candidates instead.

        # Keywords that suggest a real live event could be happening
        _LIVE_EVENT_KEYWORDS = {
            # Sports
            "nfl", "nba", "mlb", "nhl", "ufc", "mma", "soccer", "football",
            "basketball", "baseball", "hockey", "tennis", "golf", "f1",
            "formula", "nascar", "super bowl", "world series", "stanley cup",
            "playoffs", "finals", "championship", "tournament", "match",
            "game", "fight", "bout", "race", "open", "wimbledon", "vs",
            "score", "goals", "o/u", "over", "under", "points", "assists",
            "rebounds", "corners", "shots", "winner", "result",
            # Politics / geopolitics
            "debate", "speech", "conference", "summit", "hearing", "trial",
            "vote", "election", "inauguration", "press", "ruling", "verdict",
            "senate", "congress", "president", "trump", "iran", "russia",
            "ukraine", "china", "taiwan", "ceasefire", "sanctions",
            "announce", "decision", "report",
            # Crypto / finance (same-day price/rate events)
            "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
            "price", "cpi", "inflation", "fed", "fomc", "interest rate",
            "gdp", "jobs report", "nonfarm", "earnings", "revenue",
            "s&p", "nasdaq", "dow", "launch",
            # Entertainment (award shows, release-day events)
            "oscar", "emmy", "grammy", "golden globe", "bafta",
            "box office", "chart", "billboard", "album", "release",
            # Weather / science
            "hurricane", "tornado", "storm", "earthquake", "wildfire",
            "temperature", "weather",
        }

        def _could_be_live(title: str) -> bool:
            """Return True if the title suggests a real-time event might be live."""
            t = title.lower()
            return any(kw in t for kw in _LIVE_EVENT_KEYWORDS)

        expiring_kalshi_raw = []  # closing soon but NOT confirmed live
        expiring_poly_raw   = []  # same for Poly
        live_kalshi_raw     = []  # confirmed live — actual game/match in progress
        live_poly_raw       = []  # confirmed live on Polymarket

        # ── Kalshi: use Kalshi's own live event API first (most accurate) ────────
        # Kalshi shows "LIVE 38" in their nav — these are real in-progress events
        # like "Czechia vs Guatemala 74'" — not just markets closing soon.
        try:
            kalshi_live_now = await kalshi.get_live_now_markets(max_markets=50)
            live_kalshi_raw = kalshi_live_now
            logger.info("Kalshi live now: %d confirmed in-play markets", len(live_kalshi_raw))
        except Exception as _le:
            logger.debug("Kalshi live now fetch skipped: %s", _le)

        # ── Polymarket: use Poly's own live/sports API first ────────────────────
        # Polymarket shows NHL, MLB, WNBA, Tennis live in their app.
        # get_live_now_markets() hits the Gamma API with live=true + sport tags.
        poly_time_raw = []
        if poly_enabled:
            try:
                live_poly_raw = await poly_client.get_live_now_markets(max_markets=50)
                logger.info("Polymarket live now: %d confirmed in-play markets", len(live_poly_raw))
            except Exception as _le:
                logger.debug("Polymarket live now fetch skipped: %s", _le)
            try:
                poly_time_raw = await poly_client.get_live_markets(max_hours=24.0, max_markets=50)
            except Exception as _le:
                logger.debug("Live Polymarket fetch skipped: %s", _le)

        # Non-live Poly markets → expiring pool
        live_poly_tickers = {m.get("ticker") for m in live_poly_raw}
        for m in poly_time_raw:
            if m.get("ticker") not in live_poly_tickers:
                expiring_poly_raw.append(m)

        # All Kalshi time-window markets that aren't in the live set → expiring
        try:
            kalshi_24h_raw = await kalshi.get_live_markets(max_hours=24.0, max_markets=50)
        except Exception:
            kalshi_24h_raw = []
        live_k_tickers = {m.get("ticker") for m in live_kalshi_raw}
        for m in kalshi_24h_raw:
            if m.get("ticker") not in live_k_tickers:
                expiring_kalshi_raw.append(m)

        live_kalshi = [
            m for m in live_kalshi_raw
            if not _already_open(m)
            and m.get("ticker") not in arb_tickers
            and 2 < _tradeable_price(m) < 98
            and (m.get("title") or "")
        ]
        if live_kalshi_raw and not live_kalshi:
            no_price = [m.get("ticker","?") for m in live_kalshi_raw if not (2 < _tradeable_price(m) < 98)]
            logger.info("Kalshi live: %d raw → 0 tradeable (price filter dropped %d: %s)",
                        len(live_kalshi_raw), len(no_price), no_price[:5])
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

        # Feed live markets into daily_stats so Discord heartbeat shows them
        from src.utils.daily_stats import stats as _ds
        _all_live = live_kalshi + live_poly
        _ds.update_scan_state(
            live_markets=_all_live,
            regular_top=list(getattr(_ds, "last_regular_scan_top", [])),
        )

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
            from src.utils.confidence_calibrator import get_threshold as _get_thresh
            top_live = await live_hunter.find_top_live(
                live_markets   = all_live,
                arb_signals    = all_signals,
                min_confidence = max(_get_thresh(), settings.trading.min_ai_confidence),  # auto-calibrated daily (default 65%)
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
                    live_contracts = max(1, int(live_size / (live_price / 100))) if live_price > 0 else 0
                    live_exp_profit = live_contracts * (live_net_ev / 100) if live_net_ev is not None else None
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

                    live_platform = r["platform"]
                    live_allowed, live_reason = risk.check_trade(
                        live_tick, scaler.current_size,
                        current_positions=[], portfolio_value=portfolio_val,
                        daily_loss_override=daily_loss_db,
                        platform=live_platform,
                    )
                    if not live_allowed:
                        logger.info("LIVE SKIP %s — risk gate: %s", live_tick, live_reason)
                        results.skipped += 1
                        daily_stats.record_skip(f"live_risk_gate:{live_reason}")
                        continue
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
                        close_time=m.get("close_time", "") or m.get("expiration_time", ""),
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

        # Scan pool: markets closing TODAY only — live events, same-day results
        long_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and not _already_open(m)
            and 2 < _tradeable_price(m) < 98
            and m.get("volume", 0) > 0                    # hard reject zero-volume markets
            and m.get("volume", 0) >= max(min_vol, 10)
            and (m.get("title") or "")
            and m.get("close_time")
            and _closes_today(m)                          # today only — live events
            and not is_junk(m.get("title", ""))
        ]

        # Short-duration pool: closes within 24h — lower volume ok (new daily markets)
        short_term = [
            m for m in markets
            if m.get("ticker") not in arb_tickers
            and not _already_open(m)
            and 2 < _tradeable_price(m) < 98
            and m.get("volume", 0) > 0                    # hard reject zero-volume markets
            and m.get("ticker") not in {x.get("ticker") for x in long_term}
            and m.get("close_time")                       # must have a close time set
            and _closes_today(m)
            and (m.get("title") or "")
            and not is_junk(m.get("title", ""))
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
                and m.get("volume", 0) > 0                     # hard reject zero-volume
                and m.get("close_time")
                and _closes_today(m)                           # today only
                and not is_junk(m.get("title", ""))
            ]
            cat_poly = [
                m for m in cat_markets
                if m.get("platform") == "polymarket"
                and not _already_open(m)
                and m.get("yes_ask", 0) > 1
                and m.get("volume", 0) > 0                     # hard reject zero-volume
                and m.get("close_time")
                and _closes_today(m)                           # today only
                and not is_junk(m.get("title", ""))
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
        # Must pass the same today-only + volume filters as the main pools
        existing_all_tickers = {m.get("ticker") for m in long_term + short_term + live_kalshi + poly_markets}
        for m in expiring_kalshi_raw:
            if (m.get("ticker") not in existing_all_tickers
                    and not _already_open(m)
                    and 2 < _tradeable_price(m) < 98
                    and m.get("volume", 0) > 0
                    and _closes_today(m)):             # today only — no tomorrow bleed-through
                long_term.append(m)
                existing_all_tickers.add(m.get("ticker"))
        for m in expiring_poly_raw:
            if (m.get("ticker") not in existing_all_tickers
                    and not _already_open(m)
                    and m.get("yes_ask", 0) > 1
                    and m.get("volume", 0) > 0
                    and _closes_today(m)):             # today only
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

        # Step 6: cross-platform dedup — same event on Kalshi + Polymarket → keep Kalshi
        import re as _xre
        def _event_key(title: str) -> str:
            t = _xre.sub(r'[^a-z0-9 ]', ' ', (title or "").lower())
            words = [w for w in t.split() if len(w) > 3 and w not in
                     {"will","that","this","with","from","have","been","they","were","when","what","which"}]
            return " ".join(sorted(words[:8]))
        # Use a set to avoid dict-collision dropping valid Kalshi keys
        _kalshi_event_key_set = {ek for m in kalshi_candidates
                                  if (ek := _event_key(m.get("title",""))) and len(ek) > 4}
        _deduped_poly = []
        for m in poly_markets:
            ek = _event_key(m.get("title",""))
            if ek and len(ek) > 4 and ek in _kalshi_event_key_set:
                logger.debug("Cross-platform dedup: Poly %s matches Kalshi event — keeping Kalshi",
                             m.get("ticker","")[:30])
            else:
                _deduped_poly.append(m)
        poly_markets = _deduped_poly

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

        # Update regular scan top picks for Discord heartbeat
        _top_regular = kalshi_candidates[:6] + poly_markets[:6]
        _ds.update_scan_state(
            live_markets=list(getattr(_ds, "last_live_scan_markets", [])),
            regular_top=_top_regular,
        )

        if not best:
            # Log reason; no_opportunity() Discord method is disabled (returns immediately)
            # — covered by the hourly heartbeat instead. Also suppress if positions are open.
            logger.info(
                "No best opportunity found across %d Kalshi + %d Polymarket candidates "
                "(open_positions=%d)",
                len(kalshi_candidates), len(poly_markets), open_count,
            )
        elif best:
            # Enforce tiered confidence by time-to-close:
            #   today (≤24h)  → 77% min
            #   2–7 days      → 77% min  (same bar — no lowering for time-sensitive markets)
            _best_market = best.get("market", {})
            _best_conf   = float(best.get("decision", {}).get("confidence", 0))
            _best_ct     = _best_market.get("close_time", "")
            _hours_out   = 999.0
            _now_et_fresh = _dt.now(_ET)  # fresh timestamp — cycle may have taken 10-30s
            try:
                _close_dt = _dt.fromisoformat(str(_best_ct).replace("Z", "+00:00"))
                if _close_dt.tzinfo is None:
                    _close_dt = _close_dt.replace(tzinfo=_tz.utc).astimezone(_ET)
                _hours_out = (_close_dt - _now_et_fresh).total_seconds() / 3600
            except Exception:
                pass
            # Bid on markets closing within 48h — not just today
            if _hours_out > 48:
                logger.info(
                    "Best opportunity WATCHING — %s closes in %.0fh (>48h) — will bid closer to event",
                    _best_market.get("ticker", "?"), _hours_out,
                )
                daily_stats.record_skip("watching_not_soon")
                best = None

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
            #   70–79%  → half size  (50% of base) — WATCH only, no bid
            #   80–87%  → full size  (100% of base) — BID standard
            #   88%+    → max size   (150% of base, capped) — BID full Kelly
            confidence = float(decision.get("confidence", 0))
            base       = scaler.current_size
            if confidence < settings.trading.min_ai_confidence:
                logger.info("Best opportunity SKIPPED — confidence %.0f%% below min %.0f%%", confidence, settings.trading.min_ai_confidence)
                daily_stats.record_skip(f"confidence_below_min:{confidence:.0f}%")
                best = None
            elif confidence < 80:
                # 70-79%: WATCH only — fire Discord alert but DO NOT place a bid
                logger.info("WATCH ONLY %.0f%% conf — no bid placed for %s", confidence, ticker)
                try:
                    await discord.bot_alert(
                        picks=[{**market, "action": decision.get("action","BUY"),
                                "side": side, "confidence": confidence,
                                "reasoning": decision.get("reasoning",""),
                                "price_cents": price, "is_live": bool(market.get("is_live"))}],
                        mode="📝 PAPER" if not live_mode else "💰 LIVE",
                    )
                except Exception as _wa_err:
                    logger.debug("Watch alert error: %s", _wa_err)
                best = None
                results.skipped += 1
                daily_stats.record_skip(f"watch_only:{confidence:.0f}%")
            elif confidence >= 88:
                size_multiplier = 1.5
                size_tier       = "MAX (88%+ conf)"
            elif confidence >= 80:
                size_multiplier = 1.0
                size_tier       = "FULL (80–87% conf)"
            if best is None:
                planned_size_usd = 0
            else:
                planned_size_usd = round(max(min_size, min(base * size_multiplier, max_size)), 2)
            if best is not None:
                is_live_market   = bool(market.get("is_live"))
                logger.info("Trade size: $%.2f [%s]%s", planned_size_usd, size_tier,
                            " [LIVE]" if is_live_market else "")
                contracts_est  = (planned_size_usd / (price / 100)) if price > 0 else 0
                if net_ev is not None and net_ev > 0:
                    exp_profit_usd = contracts_est * (net_ev / 100)
                else:
                    # No EV from AI — estimate from confidence: treat conf-65 as edge
                    edge = max(0.0, confidence - settings.trading.min_ai_confidence) * 0.002  # 70%→$0.00, 80%→$0.02, 88%→$0.036
                    exp_profit_usd = planned_size_usd * edge if edge > 0 else None
                roi_pct = (exp_profit_usd / planned_size_usd * 100) if (exp_profit_usd and planned_size_usd) else None

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
                    _best_platform = best.get("platform", "kalshi")
                    allowed, reason = risk.check_trade(
                        ticker, scaler.current_size,
                        current_positions=[], portfolio_value=portfolio_val,
                        daily_loss_override=daily_loss_db,
                        platform=_best_platform,
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

                        # Route to the correct platform's trader
                        # BOT ALERT fires inside execute() before BID PLACED
                        platform = best.get("platform", "kalshi")
                        active_trader = kalshi_trader if platform == "kalshi" else poly_trader

                        # GAP 2: proactive order book open check before placing bid
                        _ob_blocked = False
                        if platform == "kalshi":
                            try:
                                _mkt_check = await kalshi.get_market(ticker)
                                _mkt_status = (_mkt_check.get("market") or _mkt_check).get("status", "open")
                                if _mkt_status not in ("open", "active", ""):
                                    logger.warning("ORDER BOOK CLOSED pre-check — %s status=%s, skipping", ticker, _mkt_status)
                                    daily_stats.record_skip(f"order_book_closed:{ticker}")
                                    results.skipped += 1
                                    _ob_blocked = True
                            except Exception as _ob_err:
                                logger.debug("Order book pre-check error for %s: %s", ticker, _ob_err)

                        rec = None
                        if not _ob_blocked:
                            try:
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
                                    close_time=market.get("close_time", "") or market.get("expiration_time", ""),
                                    **({"poly_token_id": market.get("_yes_token") if side == "yes" else market.get("_no_token")}
                                       if platform == "polymarket" else {}),
                                )
                            except Exception as _exec_err:
                                _err_str = str(_exec_err).lower()
                                if any(k in _err_str for k in ("order book", "orderbook", "closed", "not tradeable", "trading halted", "market not open")):
                                    logger.warning("ORDER BOOK CLOSED — skipping %s: %s", ticker, _exec_err)
                                    daily_stats.record_skip(f"order_book_closed:{ticker}")
                                else:
                                    logger.error("Trade execution failed for %s: %s", ticker, _exec_err)
                                rec = None
                        if rec:
                            trades_this_cycle += 1
                            results.total_positions += 1
                            results.total_capital_used += rec.get("total_cost", 0)
                            results.ai_trades += 1
                            daily_stats.record_trade(
                                ticker=ticker, side=side,
                                confidence=float(decision.get("confidence", 0)),
                                net_ev=net_ev, score=best.get("_pre_score", 0),
                                reasoning=(decision.get("reasoning") or "")[:200],
                                title=market.get("title", "") or ticker,
                            )
                            # Mark this traded market as a live event in the heartbeat
                            _traded_market = dict(market)
                            _traded_market["_just_traded"] = True
                            _existing_live = list(getattr(_ds, "last_live_scan_markets", []))
                            if not any(m.get("ticker") == ticker for m in _existing_live):
                                _existing_live.insert(0, _traded_market)
                            _ds.update_scan_state(
                                live_markets=_existing_live[:6],
                                regular_top=list(getattr(_ds, "last_regular_scan_top", [])),
                            )
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

    # ── Real-time position resolution (runs every 45s) ────────────────────
    # As soon as close_time passes → result recorded → Discord fires → deleted
    try:
        await _resolve_expired_positions(db, live_mode, risk=risk)
    except Exception as _re:
        logger.debug("Real-time resolve error: %s", _re)

    return results


async def _resolve_expired_positions(db, live_mode: bool = False, risk=None) -> None:
    """
    Check all open positions whose market close_time has passed.
    Record result in trade_logs, fire Discord W/L alert, hard-delete from positions.
    Runs every 45s trade cycle — real-time resolution, no waiting for heartbeat.
    """
    from datetime import datetime as _r_dt
    from zoneinfo import ZoneInfo as _r_ZI
    _r_ET = _r_ZI("America/New_York")
    def _et_now_str() -> str:
        return _r_dt.now(_r_ET).strftime("%Y-%m-%dT%H:%M:%S")
    # Add close_time column to positions if missing (migration)
    try:
        await db.execute("ALTER TABLE positions ADD COLUMN close_time TEXT DEFAULT ''")
    except Exception:
        pass

    expired = await db.fetchall(
        "SELECT p.ticker, p.title, p.platform, p.side, p.contracts, "
        "       p.avg_price, p.opened_at, p.poly_token_id, "
        "       m.yes_ask, m.no_ask, "
        "       COALESCE(NULLIF(m.close_time,''), NULLIF(p.close_time,'')) AS close_time "
        "FROM positions p "
        "LEFT JOIN markets m ON m.ticker = p.ticker "
        "WHERE p.status='open' "
        "AND COALESCE(NULLIF(m.close_time,''), NULLIF(p.close_time,'')) IS NOT NULL "
        "AND datetime(substr(replace(replace(COALESCE(NULLIF(m.close_time,''), NULLIF(p.close_time,'')),'Z',''),'T',' '),1,19)) < datetime('now')"
    )

    if not expired:
        return

    try:
        from src.alerts.discord import DiscordAlerter
        discord = DiscordAlerter()
        mode = "PAPER" if not live_mode else "LIVE"
    except Exception:
        discord = None
        mode = "PAPER"

    resolved_wins   = []
    resolved_losses = []
    resolved_exits  = []
    _seen_tickers: set = set()

    for pos in expired:
        ticker        = pos.get("ticker", "")
        title         = pos.get("title") or ticker
        platform      = pos.get("platform", "kalshi")
        side          = pos.get("side", "yes")
        contracts     = int(pos.get("contracts") or 1)
        entry         = float(pos.get("avg_price") or 0)
        poly_token_id = pos.get("poly_token_id") or ""
        # Use the correct side's ask price — NO positions exit at no_ask, not yes_ask
        exit_p    = float(pos.get("no_ask" if side == "no" else "yes_ask") or 0)

        # If DB price is stale (0), fetch live resolution from Kalshi API
        if exit_p == 0 and entry > 0 and platform != "polymarket":
            try:
                from src.clients.kalshi_client import KalshiClient
                _kc = KalshiClient()
                _resp = await _kc.get_market(ticker)
                await _kc.close()
                _mkt = _resp.get("market", _resp) if isinstance(_resp, dict) else {}
                if _mkt:
                    _yes = float(_mkt.get("yes_ask") or _mkt.get("last_price") or 0)
                    _no  = float(_mkt.get("no_ask") or 0)
                    if _yes == 100 or _no == 100 or _yes > 0:
                        exit_p = _no if side == "no" else _yes
            except Exception as _fe:
                logger.debug("Live Kalshi price fetch for %s: %s", ticker, _fe)

        # Polymarket: fetch live price via token_id (ticker is a hex token ID)
        if exit_p == 0 and entry > 0 and platform == "polymarket":
            _token = poly_token_id or ticker
            try:
                from src.clients.polymarket_client import PolymarketTradingClient
                _pc = PolymarketTradingClient()
                _pmkt = await _pc.get_market_by_token(_token)
                await _pc.close()
                if _pmkt:
                    _yes = float(_pmkt.get("yes_ask") or _pmkt.get("last_price") or 0)
                    _no  = float(_pmkt.get("no_ask") or 0)
                    if _yes == 100 or _no == 100 or _yes > 0:
                        exit_p = _no if side == "no" else _yes
            except Exception as _pfe:
                logger.debug("Live Poly price fetch for %s: %s", _token, _pfe)

        # If still 0 after all fetches — force-close as EXPIRED so it leaves the board
        if exit_p == 0 and entry > 0:
            logger.info("FORCE-CLOSE %s — no exit price after live fetch, marking EXPIRED", ticker)
            await db.execute(
                "DELETE FROM positions WHERE ticker=? AND status='open'", (ticker,)
            )
            # Sync live slot manager so phantom slots don't block new fills
            try:
                from src.jobs.live_market_manager import _live_slots as _lslots
                _lslots.pop(ticker, None)
            except Exception:
                pass
            _tl_row = await db.fetchone(
                "SELECT id FROM trade_logs WHERE ticker=? AND (platform=? OR platform IS NULL) "
                "AND (pnl IS NULL OR pnl=0) ORDER BY executed_at DESC LIMIT 1", (ticker, platform)
            )
            if _tl_row and _tl_row.get("id"):
                await db.execute(
                    "UPDATE trade_logs SET result='EXPIRED', resolved_at=?, pnl=0 WHERE id=?",
                    (_et_now_str(), _tl_row["id"])
                )
            continue

        # Guard for entry == 0 edge case (unresolvable position)
        if exit_p == 0 and entry == 0:
            logger.warning("SKIP %s — both exit_p and entry are 0, position unresolvable", ticker)
            continue

        pnl_cents = (exit_p - entry) * contracts
        # Deduct Kalshi taker fee paid at entry (2% of entry cost)
        if platform != "polymarket":
            entry_fee_usd = (entry / 100.0) * contracts * 0.02
        else:
            entry_fee_usd = 0.0
        pnl_usd   = (pnl_cents / 100.0) - entry_fee_usd
        result    = "WIN" if pnl_usd > 0 else ("LOSS" if pnl_usd < 0 else "BREAK_EVEN")

        # Stamp into trade_logs (permanent W/L record)
        try:
            # SQLite doesn't support ORDER BY/LIMIT in UPDATE — select rowid first
            _tl_row = await db.fetchone(
                "SELECT id FROM trade_logs WHERE ticker=? AND (platform=? OR platform IS NULL) "
                "AND (pnl IS NULL OR pnl=0) ORDER BY executed_at DESC LIMIT 1",
                (ticker, platform)
            )
            if _tl_row and _tl_row.get("id"):
                await db.execute(
                    "UPDATE trade_logs SET pnl=?, resolved_at=?, "
                    "result=?, exit_price=? WHERE id=?",
                    (pnl_usd, _et_now_str(), result, exit_p, _tl_row["id"])
                )
        except Exception as _tl:
            logger.debug("trade_logs update %s: %s", ticker, _tl)

        # Update in-memory circuit breaker
        if risk:
            risk.record_result(ticker, pnl_usd, platform)

        # Hard delete — off the board
        await db.execute(
            "DELETE FROM positions WHERE ticker=? AND status='open'",
            (ticker,)
        )
        # Sync live slot manager so phantom slots don't block new fills
        try:
            from src.jobs.live_market_manager import _live_slots as _lslots
            _lslots.pop(ticker, None)
        except Exception:
            pass

        logger.info(
            "RESOLVED [%s] %s → %s | entry=%.0f¢ exit=%.0f¢ pnl=$%.2f",
            platform.upper(), ticker, result, entry, exit_p, pnl_usd
        )

        item = {
            "title":     title[:50],
            "ticker":    ticker,
            "side":      side.upper(),
            "platform":  platform,
            "entry":     entry,
            "exit":      exit_p,
            "pnl":       pnl_usd,
            "contracts": contracts,
        }
        if result == "WIN":
            resolved_wins.append(item)
        else:
            resolved_losses.append(item)
        _seen_tickers.add(ticker)

    # Sweep positions closed by track.py in the last 5 minutes so wins and
    # opt-outs that track.py resolves first also appear in LIVE BID RESULTS.
    try:
        recent_track_closes = await db.fetchall(
            "SELECT ticker, title, platform, side, avg_price, pnl, contracts, close_reason "
            "FROM positions "
            "WHERE status='closed' AND closed_at >= datetime('now', '-5 minutes') "
            "AND close_reason IS NOT NULL"
        )
        for _rc in (recent_track_closes or []):
            _rt = _rc.get("ticker", "")
            if _rt in _seen_tickers:
                continue  # already handled above
            _seen_tickers.add(_rt)
            _reason  = _rc.get("close_reason", "")
            _pnl_rc  = float(_rc.get("pnl") or 0)
            _item_rc = {
                "title":     (_rc.get("title") or _rt)[:50],
                "ticker":    _rt,
                "side":      (_rc.get("side") or "YES").upper(),
                "platform":  _rc.get("platform", "kalshi"),
                "entry":     float(_rc.get("avg_price") or 0),
                "exit":      0,
                "pnl":       _pnl_rc,
                "contracts": int(_rc.get("contracts") or 1),
                "reason":    _reason,
            }
            if _reason.startswith("ai_reeval"):
                resolved_exits.append(_item_rc)
            elif _pnl_rc >= 0:
                resolved_wins.append(_item_rc)
            else:
                resolved_losses.append(_item_rc)
    except Exception as _sweep_err:
        logger.debug("Track-close sweep error: %s", _sweep_err)

    # Immediate Discord W/L notification
    if discord and (resolved_wins or resolved_losses or resolved_exits):
        try:
            await discord.live_results_summary(
                wins=resolved_wins,
                losses=resolved_losses,
                exits=resolved_exits,
                mode=mode,
            )
        except Exception as _da:
            logger.debug("Discord result alert: %s", _da)

    logger.info(
        "Real-time resolve: %dW / %dL / %d exits — cleared from board, W/L updated",
        len(resolved_wins), len(resolved_losses), len(resolved_exits)
    )
