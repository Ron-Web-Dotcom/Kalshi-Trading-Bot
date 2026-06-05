#!/usr/bin/env python3
"""
Kalshi AI Trading Bot — main entry point.

Usage:
    python bot.py                    # Paper trading (safe default)
    python bot.py --live             # Live trading (requires LIVE_TRADING_ENABLED=true in .env)
    python bot.py --once             # One cycle then exit (good for testing)
    python bot.py --log-level DEBUG  # Verbose output
"""

import asyncio
import argparse
import logging
import signal
import sys
from datetime import datetime, timezone

from src.utils.logging_setup import setup_logging, get_trading_logger
from src.utils.database import DatabaseManager
from src.jobs.ingest import run_ingestion
from src.jobs.trade import run_trading_job
from src.jobs.track import run_tracking
from src.config.settings import settings

logger = get_trading_logger("main")

# Cycle intervals (seconds) — bot runs 24/7 continuously
INGEST_INTERVAL    = 180   # refresh market data every 3 min
TRACK_INTERVAL     = 60    # check position PnL every 1 min
EVAL_INTERVAL      = 300   # print performance snapshot every 5 min
TRADE_INTERVAL     = 45    # run trading cycle every 45 s
HEARTBEAT_INTERVAL = 3600  # send hourly heartbeat every 60 min


class TradingBot:
    def __init__(self, live_mode: bool = False):
        # Safety: live mode requires the env var AND the CLI flag
        if live_mode and not settings.trading.live_trading_enabled:
            logger.error(
                "Cannot start LIVE: LIVE_TRADING_ENABLED=false in .env. "
                "Set it to true only after reviewing paper trade results."
            )
            sys.exit(1)

        settings.trading.live_trading_enabled = live_mode
        settings.trading.paper_trading_mode   = not live_mode

        self.live_mode  = live_mode
        self.db         = DatabaseManager()
        self._shutdown  = asyncio.Event()
        self._cycle     = 0

        # Singletons — share state across cycles so cooldowns, scaling, and
        # arb signal dedup persist without restarting (fixes A10/A11/A12)
        from src.risk.manager import RiskManager
        from src.risk.scaling import AutoScaler
        from src.strategy.arbitrage import ArbitrageDetector
        self.risk    = RiskManager(db=self.db)
        self.scaler  = AutoScaler()
        self.arb_det = ArbitrageDetector()

    def _print_startup_banner(self):
        mode  = "LIVE TRADING  ⚠️  REAL MONEY" if self.live_mode else "PAPER TRADING  ✅  No real money"
        lines = [
            "╔══════════════════════════════════════════════════╗",
            f"║  KALSHI AI TRADING BOT                           ║",
            f"║  Mode      : {mode:<36}║",
            f"║  AI model  : {settings.ai.model:<36}║",
            f"║  Base size : ${settings.trading.base_trade_size_dollars:<5.0f}  "
            f"Max: ${settings.trading.max_trade_size_dollars:<5.0f}  "
            f"Kelly: {settings.trading.kelly_fraction:.0%}     ║",
            f"║  Min conf  : {settings.trading.min_ai_confidence:.0f}%   "
            f"Arb threshold: {settings.trading.arbitrage_threshold_pct:.0f}%              ║",
            f"║  Daily loss cap : {settings.trading.max_daily_loss_pct:.0f}%   "
            f"Cooldown: {settings.trading.cooldown_between_trades_seconds}s              ║",
            "╚══════════════════════════════════════════════════╝",
        ]
        for line in lines:
            logger.info(line)

    async def startup(self):
        # Run health check before startup banner
        health = {}
        try:
            from src.utils.health_check import HealthChecker
            checker = HealthChecker()
            health = await checker.run_all()
        except Exception as _he:
            logger.warning("Health check failed: %s", _he)

        self._print_startup_banner()
        logger.info("Initializing database...")
        await self.db.initialize()
        logger.info("Running initial market ingestion...")
        try:
            count = await run_ingestion(self.db)
            logger.info("Initial ingestion complete: %d markets cached", count)
        except Exception as e:
            logger.warning("Initial ingestion failed (will retry): %s", e)

        # Discord startup alert
        try:
            from src.alerts.discord import DiscordAlerter
            discord = DiscordAlerter()
            balance = None
            if self.live_mode:
                try:
                    from src.clients.kalshi_client import KalshiClient
                    k = KalshiClient()
                    bal = await k.get_balance()
                    balance = (bal.get("balance") or 0) / 100
                    await k.close()
                except Exception:
                    pass
            await discord.startup_banner(
                mode="LIVE" if self.live_mode else "PAPER",
                balance=balance,
                health_results=health,
            )
        except Exception:
            pass

    async def run_cycle(self):
        self._cycle += 1
        logger.info(
            "┌── Trading Cycle #%d ─────────────────────────────────────",
            self._cycle,
        )
        results = await run_trading_job(
            db=self.db, risk=self.risk, scaler=self.scaler, arb_det=self.arb_det
        )
        logger.info(
            "└── Cycle #%d done: %d trade(s) (arb=%d ai=%d skip=%d) $%.2f capital",
            self._cycle,
            results.total_positions,
            results.arb_trades,
            results.ai_trades,
            results.skipped,
            results.total_capital_used,
        )

    async def run_loop(self):
        await self.startup()

        # ── Sleep mode: 3:00–5:00 AM ET — bot pauses all scanning ───────────
        # Returns seconds until sleep window ends (0 if not in window).
        async def _sleep_mode_wait() -> bool:
            """If in quiet hours (3–5am ET), sleep until 5am and return True."""
            from src.utils.eastern_time import now_et as _net
            from datetime import timedelta as _td
            et = _net()
            if 3 <= et.hour < 5:
                wake = et.replace(hour=5, minute=0, second=0, microsecond=0)
                secs = (wake - et).total_seconds()
                logger.info("😴 Sleep mode: quiet hours 3–5am ET — resuming at 5:00am (%.0f min)", secs / 60)
                await asyncio.sleep(secs)
                return True
            return False

        async def ingest_loop():
            while not self._shutdown.is_set():
                await asyncio.sleep(INGEST_INTERVAL)
                try:
                    count = await run_ingestion(self.db)
                    logger.info("Market refresh: %d markets", count)
                except Exception as e:
                    logger.error("Ingest error: %s", e)

        async def track_loop():
            await asyncio.sleep(TRACK_INTERVAL)  # let first cycle run first
            while not self._shutdown.is_set():
                try:
                    await run_tracking(self.db)
                except Exception as e:
                    logger.error("Track error: %s", e)
                await asyncio.sleep(TRACK_INTERVAL)

        async def trade_loop():
            while not self._shutdown.is_set():
                if await _sleep_mode_wait():
                    continue
                try:
                    await self.run_cycle()
                except Exception as e:
                    logger.error("Trade cycle error: %s", e)
                await asyncio.sleep(TRADE_INTERVAL)

        async def hourly_heartbeat_loop():
            """Send an hourly heartbeat to Discord at the top of every ET hour."""
            from src.alerts.discord import DiscordAlerter
            from src.utils.eastern_time import now_et as _now_et
            from datetime import timedelta as _hb_td
            # Sleep until the next top-of-hour ET, then fire every 3600s on the clock
            _et_now = _now_et()
            _next_hour = _et_now.replace(minute=0, second=0, microsecond=0) + _hb_td(hours=1)
            await asyncio.sleep((_next_hour - _et_now).total_seconds())
            while not self._shutdown.is_set():
                try:
                    discord = DiscordAlerter()
                    today = _now_et().date().isoformat()

                    # Total markets in DB
                    # Kalshi vs Polymarket split — open markets only
                    kal_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE (platform='kalshi' OR platform IS NULL) AND (status='open' OR status='')"
                    )
                    poly_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='polymarket' AND (status='open' OR status='')"
                    )
                    kalshi_count  = (kal_row  or {}).get("n", 0)
                    poly_count    = (poly_row or {}).get("n", 0)
                    markets_total = kalshi_count + poly_count

                    # Open positions — ALL platforms (Kalshi + Polymarket), live + regular
                    open_rows = await self.db.fetchall(
                        "SELECT platform, COUNT(*) as n FROM positions "
                        "WHERE status='open' GROUP BY platform"
                    )
                    open_n = sum((r.get("n") or 0) for r in (open_rows or []))
                    open_by_platform = {r.get("platform", "kalshi"): r.get("n", 0) for r in (open_rows or [])}
                    # Live in-play tickers from live slot manager
                    from src.jobs.live_market_manager import _live_slots as _hb_ls
                    live_tickers = set(_hb_ls.keys())
                    # Count live open positions across both platforms
                    live_open_rows = await self.db.fetchall(
                        "SELECT COUNT(*) as n FROM positions WHERE status='open' AND ticker IN ({})".format(
                            ",".join("?" * len(live_tickers))
                        ) if live_tickers else
                        "SELECT 0 as n",
                        tuple(live_tickers) if live_tickers else (),
                    )
                    live_open_n = (live_open_rows[0].get("n") or 0) if live_open_rows else 0

                    # Today's PnL (paper or live depending on mode)
                    _paper_flag = 0 if settings.trading.live_trading_enabled else 1
                    pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND pnl IS NOT NULL AND paper_trade=?",
                        (today + "T00:00:00", _paper_flag)
                    )
                    paper_pnl = (pnl_row or {}).get("pnl", 0.0)

                    # Unrealised PnL from open positions (so $0 isn't shown when trades are open)
                    unrealised_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM positions WHERE status='open'"
                    )
                    unrealised_pnl = (unrealised_row or {}).get("pnl", 0.0) or 0.0

                    # All-time win rate — the bot's track record
                    wl_row = await self.db.fetchone(
                        "SELECT "
                        "  COUNT(*) as total, "
                        "  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "  SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
                        "  COALESCE(SUM(pnl), 0) as total_pnl "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    )
                    wl = wl_row or {}
                    total_closed = wl.get("total", 0) or 0
                    total_wins   = wl.get("wins",  0) or 0
                    total_losses = wl.get("losses",0) or 0
                    total_pnl    = wl.get("total_pnl", 0.0) or 0.0
                    win_rate     = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

                    # Top candidates: 4 from each platform, sorted by soonest expiry
                    # (rotates naturally every hour — actionable, diverse timing)
                    def _cand_rows(rows):
                        return [
                            {
                                "ticker":     r["ticker"],
                                "title":      r.get("title", ""),
                                "yes_ask":    r.get("yes_ask", 0),
                                "no_ask":     r.get("no_ask",  0),
                                "volume":     r.get("volume",  0),
                                "platform":   r.get("platform", "kalshi"),
                                "close_time": r.get("close_time", ""),
                            }
                            for r in (rows or [])
                        ]
                    # Rotate candidates each hour — exclude tickers shown last time
                    _shown_last = getattr(self, "_hb_shown_tickers", set())
                    _exclude_sql = ""
                    _exclude_params: tuple = ()
                    if _shown_last:
                        placeholders = ",".join("?" * len(_shown_last))
                        _exclude_sql = f"AND ticker NOT IN ({placeholders}) "
                        _exclude_params = tuple(_shown_last)

                    kal_cand = await self.db.fetchall(
                        "SELECT ticker, title, yes_ask, no_ask, volume, platform, close_time FROM markets "
                        "WHERE yes_ask > 5 AND yes_ask < 95 "
                        "AND (platform='kalshi' OR platform IS NULL) "
                        "AND (status='open' OR status='') "
                        "AND title IS NOT NULL AND title != '' AND title NOT LIKE '0x%' "
                        + _exclude_sql +
                        "ORDER BY ABS(yes_ask - 50) ASC, volume DESC LIMIT 6",
                        _exclude_params,
                    )
                    # Fallback — if exclusion left nothing, show any fresh markets
                    if not kal_cand:
                        kal_cand = await self.db.fetchall(
                            "SELECT ticker, title, yes_ask, no_ask, volume, platform, close_time FROM markets "
                            "WHERE yes_ask > 5 AND yes_ask < 95 "
                            "AND (platform='kalshi' OR platform IS NULL) "
                            "AND (status='open' OR status='') "
                            "AND title IS NOT NULL AND title != '' AND title NOT LIKE '0x%' "
                            "ORDER BY RANDOM() LIMIT 6"
                        )
                    poly_cand = await self.db.fetchall(
                        "SELECT ticker, title, yes_ask, no_ask, volume, platform, close_time FROM markets "
                        "WHERE yes_ask > 5 AND yes_ask < 95 "
                        "AND platform='polymarket' "
                        "AND (status='open' OR status='') "
                        "AND title IS NOT NULL AND title != '' AND title NOT LIKE '0x%' "
                        + _exclude_sql +
                        "ORDER BY ABS(yes_ask - 50) ASC, volume DESC LIMIT 6",
                        _exclude_params,
                    )
                    if not poly_cand:
                        poly_cand = await self.db.fetchall(
                            "SELECT ticker, title, yes_ask, no_ask, volume, platform, close_time FROM markets "
                            "WHERE yes_ask > 5 AND yes_ask < 95 "
                            "AND platform='polymarket' "
                            "AND (status='open' OR status='') "
                            "AND title IS NOT NULL AND title != '' AND title NOT LIKE '0x%' "
                            "ORDER BY RANDOM() LIMIT 6"
                        )
                    top_candidates = _cand_rows(kal_cand) + _cand_rows(poly_cand)
                    # Remember what we showed so next hour rotates to fresh ones
                    self._hb_shown_tickers = {c["ticker"] for c in top_candidates}

                    # Today's closed trades with outcomes
                    closed_rows = await self.db.fetchall(
                        "SELECT ticker, side, pnl, close_reason FROM positions "
                        "WHERE status='closed' AND closed_at >= ? ORDER BY closed_at DESC LIMIT 10",
                        (today + "T00:00:00",)
                    )
                    closed_trades = [dict(r) for r in (closed_rows or [])]

                    from src.utils.daily_stats import stats as daily_stats
                    from src.jobs.live_market_manager import _live_slots, MAX_LIVE_POSITIONS
                    await discord.hourly_heartbeat(
                        markets_scanned=markets_total,
                        kalshi_count=kalshi_count,
                        poly_count=poly_count,
                        top_candidates=top_candidates,
                        open_positions=open_n,
                        live_open_positions=live_open_n,
                        open_by_platform=open_by_platform,
                        paper_pnl=paper_pnl,
                        unrealised_pnl=unrealised_pnl,
                        paper=not settings.trading.live_trading_enabled,
                        closed_trades=closed_trades,
                        win_rate=win_rate,
                        total_wins=total_wins,
                        total_losses=total_losses,
                        total_pnl=total_pnl,
                        total_closed=total_closed,
                        best_pick=daily_stats.best_pick(),
                        live_slots=len(_live_slots),
                        live_slots_max=MAX_LIVE_POSITIONS,
                        all_evaluations=list(daily_stats.all_evaluations),
                    )
                except Exception as e:
                    logger.error("Hourly heartbeat error: %s", e)

                # Sleep until the next top-of-hour ET
                _et_now = _now_et()
                _next_hour = _et_now.replace(minute=0, second=0, microsecond=0) + _hb_td(hours=1)
                await asyncio.sleep((_next_hour - _et_now).total_seconds())

        async def daytime_summary_loop():
            """Post position digests at 12 AM, 6 AM, 12 PM, 6 PM Eastern time."""
            from src.alerts.discord import DiscordAlerter
            from src.utils.eastern_time import now_et
            from datetime import timedelta

            last_summary_at = datetime.now(timezone.utc).isoformat()

            while not self._shutdown.is_set():
                et_now = now_et()
                # Target hours in Eastern time
                targets_et = [
                    et_now.replace(hour=0,  minute=0, second=0, microsecond=0),
                    et_now.replace(hour=6,  minute=0, second=0, microsecond=0),
                    et_now.replace(hour=12, minute=0, second=0, microsecond=0),
                    et_now.replace(hour=18, minute=0, second=0, microsecond=0),
                ]
                upcoming_et = [t if t > et_now else t + timedelta(days=1) for t in targets_et]
                next_et     = min(upcoming_et)
                period      = {0: "Midnight", 6: "Morning", 12: "Afternoon", 18: "Evening"}[next_et.hour]
                secs_until  = (next_et - et_now).total_seconds()
                await asyncio.sleep(secs_until)
                if self._shutdown.is_set():
                    break

                try:
                    from datetime import timedelta as _td2
                    discord      = DiscordAlerter()
                    from src.utils.eastern_time import now_et as _now_et_sum
                    _et_sum      = _now_et_sum()
                    _paper_flag  = 0 if settings.trading.live_trading_enabled else 1

                    # At midnight (hour=0) we report on yesterday; other periods report today
                    if period == "Midnight":
                        pnl_date = (_et_sum - _td2(days=1)).date().isoformat()
                    else:
                        pnl_date = _et_sum.date().isoformat()

                    open_pos = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
                    )
                    open_pos = [dict(r) for r in (open_pos or [])]

                    new_pos = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='open' AND opened_at >= ? ORDER BY opened_at DESC",
                        (last_summary_at,)
                    )
                    new_pos = [dict(r) for r in (new_pos or [])]

                    # Positions that SETTLED (closed) since last check-in
                    closed_since = await self.db.fetchall(
                        "SELECT * FROM positions WHERE status='closed' AND closed_at >= ? ORDER BY closed_at DESC",
                        (last_summary_at,)
                    )
                    closed_since = [dict(r) for r in (closed_since or [])]

                    pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND pnl IS NOT NULL AND paper_trade=?",
                        (pnl_date + "T00:00:00", _paper_flag)
                    )
                    today_pnl = (pnl_row or {}).get("pnl", 0.0)

                    kal_row  = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE (platform='kalshi' OR platform IS NULL) AND (status='open' OR status='')"
                    )
                    poly_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM markets WHERE platform='polymarket' AND (status='open' OR status='')"
                    )
                    kalshi_count = (kal_row  or {}).get("n", 0)
                    poly_count   = (poly_row or {}).get("n", 0)

                    wl = await self.db.fetchone(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    ) or {}
                    total_closed = wl.get("total", 0) or 0
                    total_wins   = wl.get("wins",  0) or 0
                    total_losses = wl.get("losses",0) or 0
                    win_rate     = (total_wins / total_closed * 100) if total_closed > 0 else 0.0

                    from src.utils.daily_stats import stats as _ds_sum
                    from src.jobs.live_market_manager import _live_slots as _lslots
                    # Build live position list from active live slots
                    live_pos_list = [
                        dict(v, ticker=k) for k, v in _lslots.items()
                    ]

                    await discord.daytime_summary(
                        period=period,
                        open_positions=open_pos,
                        new_positions=new_pos,
                        today_pnl=today_pnl,
                        kalshi_count=kalshi_count,
                        poly_count=poly_count,
                        win_rate=win_rate,
                        total_wins=total_wins,
                        total_losses=total_losses,
                        total_closed=total_closed,
                        paper=not settings.trading.live_trading_enabled,
                        closed_since_last=closed_since,
                        best_buys=list(_ds_sum.all_evaluations),
                        live_positions=live_pos_list,
                    )
                    # Missed trades — separate message at same 4 scheduled times only
                    try:
                        await discord.near_miss_digest(
                            paper=not settings.trading.live_trading_enabled
                        )
                    except Exception:
                        pass
                    # Open position monitor — separate message at same 4 scheduled times only
                    try:
                        all_pos = await self.db.fetchall(
                            "SELECT * FROM positions WHERE status='open' ORDER BY opened_at DESC"
                        )
                        all_pos = [dict(r) for r in (all_pos or [])]
                        if all_pos:
                            await discord.position_monitor(
                                positions=all_pos,
                                paper=not settings.trading.live_trading_enabled,
                            )
                    except Exception:
                        pass
                except Exception as e:
                    logger.error("Daytime summary error: %s", e)
                finally:
                    last_summary_at = datetime.now(timezone.utc).isoformat()

        async def daily_summary_loop():
            """Post midnight daily report to Discord, then reset daily stats."""
            from src.alerts.discord import DiscordAlerter
            from src.utils.eastern_time import now_et
            from src.utils.daily_stats import stats as daily_stats

            while not self._shutdown.is_set():
                # Sleep until next midnight Eastern time
                et_now       = now_et()
                midnight_et  = et_now.replace(hour=0, minute=0, second=0, microsecond=0)
                from datetime import timedelta
                next_midnight = midnight_et + timedelta(days=1)
                secs_until    = (next_midnight - et_now).total_seconds()
                await asyncio.sleep(secs_until + 120)  # +2 min to avoid collision with daytime summary

                try:
                    from datetime import timedelta as _td
                    discord      = DiscordAlerter()
                    _et_now      = now_et()
                    # Report covers the day that just ended — use yesterday's date
                    report_date  = (_et_now - _td(days=1)).date().isoformat()
                    snap         = daily_stats.snapshot()
                    paper        = not settings.trading.live_trading_enabled
                    _paper_flag  = 0 if settings.trading.live_trading_enabled else 1

                    # Win/loss record (all-time — no date filter)
                    wl = await self.db.fetchone(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
                        "COALESCE(SUM(pnl),0) as total_pnl "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    ) or {}
                    # Today's (yesterday's) realized PnL — query the day that just ended
                    today_pnl_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM trade_logs "
                        "WHERE executed_at >= ? AND executed_at < ? AND pnl IS NOT NULL AND paper_trade=?",
                        (report_date + "T00:00:00", report_date + "T23:59:59", _paper_flag)
                    ) or {}
                    closed_today = await self.db.fetchall(
                        "SELECT ticker, side, pnl, close_reason, title FROM positions "
                        "WHERE status='closed' AND closed_at >= ? AND closed_at < ? ORDER BY closed_at DESC",
                        (report_date + "T00:00:00", report_date + "T23:59:59")
                    ) or []
                    open_row = await self.db.fetchone(
                        "SELECT COUNT(*) as n FROM positions WHERE status='open'"
                    ) or {}
                    unrealised_row = await self.db.fetchone(
                        "SELECT COALESCE(SUM(pnl),0) as pnl FROM positions WHERE status='open'"
                    ) or {}
                    unrealised_pnl = float(unrealised_row.get("pnl", 0.0) or 0.0)

                    await discord.midnight_daily_summary(
                        date=report_date,
                        snap=snap,
                        wins=wl.get("wins", 0) or 0,
                        losses=wl.get("losses", 0) or 0,
                        total_closed=wl.get("total", 0) or 0,
                        alltime_pnl=wl.get("total_pnl", 0.0) or 0.0,
                        today_pnl=today_pnl_row.get("pnl", 0.0) or 0.0,
                        open_positions=open_row.get("n", 0) or 0,
                        closed_today=[dict(r) for r in closed_today],
                        paper=paper,
                        unrealised_pnl=unrealised_pnl,
                    )
                    daily_stats.reset_for_new_day()
                    logger.info("Midnight daily summary sent and stats reset.")
                except Exception as e:
                    logger.error("Daily summary error: %s", e)

                await asyncio.sleep(23 * 3600)  # safety — won't fire twice

        def _on_signal(signum, frame):
            logger.info("Shutdown signal %s — stopping bot...", signum)
            self._shutdown.set()

        signal.signal(signal.SIGINT,  _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)

        # async def discord_command_loop():
        #     """Poll Discord for bot commands every 10 seconds."""
        #     from src.utils.discord_commands import DiscordCommandListener
        #     listener = DiscordCommandListener(db=self.db)
        #     if not listener.enabled:
        #         logger.info("Discord commands disabled — set DISCORD_BOT_TOKEN + DISCORD_COMMAND_CHANNEL_ID in .env to enable")
        #         return
        #     logger.info("Discord command listener active — type !help in your channel")
        #     while not self._shutdown.is_set():
        #         try:
        #             await listener.poll_and_execute()
        #         except Exception as e:
        #             logger.debug("Discord command poll error: %s", e)
        #         await asyncio.sleep(10)

        async def live_market_manager_loop():
            """Always-on loop — maintains up to 3 live in-play positions at all times."""
            from src.jobs.live_market_manager import run_live_manager_cycle, SCAN_INTERVAL
            from src.alerts.discord import DiscordAlerter
            from src.execution.paper_trader import PaperTrader
            from src.execution.poly_paper_trader import PolyPaperTrader

            await asyncio.sleep(60)   # let ingest and trade loops warm up first
            discord_lm = DiscordAlerter()
            logger.info("Live market manager started — scanning every %ds for in-play opportunities", SCAN_INTERVAL)
            while not self._shutdown.is_set():
                try:
                    _k_trader = PaperTrader(db=self.db, discord=None, scaler=self.scaler, risk=self.risk)
                    _p_trader = PolyPaperTrader(db=self.db, discord=None, scaler=self.scaler, risk=self.risk)
                    await run_live_manager_cycle(
                        db            = self.db,
                        discord       = discord_lm,
                        settings      = settings,
                        kalshi_trader = _k_trader,
                        poly_trader   = _p_trader,
                        scaler        = self.scaler,
                        risk          = self.risk,
                    )
                except Exception as e:
                    logger.warning("Live manager loop error: %s", e, exc_info=True)
                await asyncio.sleep(SCAN_INTERVAL)

        async def manual_trade_monitor_loop():
            """Check for user-placed manual trades every 60 seconds (live mode only)."""
            from src.jobs.manual_trade_monitor import check_manual_trades
            from src.alerts.discord import DiscordAlerter
            await asyncio.sleep(30)
            while not self._shutdown.is_set():
                try:
                    discord = DiscordAlerter()
                    await check_manual_trades(db=self.db, discord=discord)
                except Exception as e:
                    logger.debug("Manual trade monitor error: %s", e)
                await asyncio.sleep(60)

        async def bot_alert_loop():
            """
            Scans every 10 min — fires Discord ONLY when there is something NEW:
              • A brand-new high-confidence live bid the bot just opted into
              • A live event bid not previously alerted (or confidence jumped a band)

            SILENT when:
              • No new picks found
              • Same picks as last time (already in _alerted at same band)
              • No live event happening right now

            Follow-up result fires ~1 min after a position closes:
              🟢 WE GOT THE BAG  🔴 WE LOST  🟡 HAVE TO OPT OUT  🟤 NEW BID
            """
            from src.alerts.discord import DiscordAlerter
            from src.utils.daily_stats import stats as _da
            from src.jobs.live_market_manager import _live_slots as _ls
            from src.data.live_event_detector import is_event_live_now

            # ticker → {band, pick_snapshot, alerted_at, result_sent}
            _alerted: dict = {}
            _alerted_date  = datetime.now(timezone.utc).date()
            # frozenset of ticker+band keys sent in last alert — prevents re-sending same bundle
            _last_sent_keys: frozenset = frozenset()

            BOT_ALERT_INTERVAL = 600   # scan every 10 min
            RESULT_CHECK_DELAY = 60    # check results 60s after alert
            MIN_CONF           = settings.trading.min_ai_confidence  # 70%

            async def _check_and_post_results(discord, mode: str):
                """
                For every previously alerted pick, check if outcome is now known
                and post a colour-coded result message (once per ticker).
                """
                now = datetime.now(timezone.utc)
                for ticker, info in list(_alerted.items()):
                    if info.get("result_sent"):
                        continue
                    alerted_at = info.get("alerted_at")
                    if not alerted_at:
                        continue
                    elapsed = (now - alerted_at).total_seconds()
                    if elapsed < RESULT_CHECK_DELAY:
                        continue

                    pick = info.get("pick", {})

                    try:
                        # Check DB for this position's outcome
                        pos = await self.db.fetchone(
                            "SELECT status, pnl, close_reason, current_price, avg_price "
                            "FROM positions WHERE ticker=? ORDER BY opened_at DESC LIMIT 1",
                            (ticker,)
                        )

                        outcome = None
                        extra   = {}

                        if pos:
                            status = pos.get("status", "")
                            pnl    = pos.get("pnl")
                            reason = pos.get("close_reason", "") or ""
                            exit_p = pos.get("current_price") or pos.get("avg_price")

                            if status == "closed" and pnl is not None:
                                extra["pnl"]        = float(pnl)
                                extra["exit_price"] = float(exit_p or 0)
                                if float(pnl) > 0:
                                    outcome = "profit"
                                    extra["result_reason"] = f"Closed at ${float(pnl):+.2f}. {reason}"
                                else:
                                    # Check if this was an early exit vs natural loss
                                    if any(k in reason.lower() for k in ["exit","early","stop","cut"]):
                                        outcome = "exit"
                                        extra["result_reason"] = f"Opted out early. {reason}"
                                    else:
                                        outcome = "loss"
                                        extra["result_reason"] = f"Closed at ${float(pnl):+.2f}. {reason}. Next one's ours."

                            elif status == "open":
                                # Position still open — check if bot just added a new entry
                                # (new bid detected = optin signal)
                                pass  # will check below

                        # If no closed position, check if a new high-conf BUY just fired
                        # on a DIFFERENT ticker (optin scenario)
                        if outcome is None:
                            for ev in list(_da.all_evaluations)[:5]:
                                if ev.get("action") != "BUY":
                                    continue
                                ev_ticker = ev.get("ticker", "")
                                ev_conf   = ev.get("confidence", 0)
                                if ev_ticker == ticker or ev_conf < 70:
                                    continue
                                if ev_ticker not in _alerted:
                                    # New high-conf pick not yet alerted — flag it as optin
                                    outcome = "optin"
                                    extra = {
                                        "ticker":        ev_ticker,
                                        "title":         ev.get("title", ""),
                                        "side":          ev.get("side", "YES"),
                                        "price_cents":   ev.get("yes_ask", 0),
                                        "confidence":    ev_conf,
                                        "net_ev":        ev.get("net_ev"),
                                        "result_reason": ev.get("reasoning", "")[:150],
                                    }
                                    break

                        if outcome:
                            result_pick = {**pick, **extra}
                            await discord.bot_alert_result(result_pick, outcome=outcome, mode=mode)
                            _alerted[ticker]["result_sent"] = True
                            logger.info("BOT ALERT RESULT fired: %s → %s", ticker, outcome)

                    except Exception as _re:
                        logger.debug("Result check error for %s: %s", ticker, _re)

            await asyncio.sleep(120)
            while not self._shutdown.is_set():
                await asyncio.sleep(BOT_ALERT_INTERVAL)
                if self._shutdown.is_set():
                    break
                if await _sleep_mode_wait():
                    continue

                try:
                    today = datetime.now(timezone.utc).date()
                    if today != _alerted_date:
                        _alerted.clear()
                        _alerted_date = today

                    discord   = DiscordAlerter()
                    mode      = "PAPER" if not settings.trading.live_trading_enabled else "LIVE"
                    now_utc   = datetime.now(timezone.utc)
                    new_picks: list = []

                    # 1. Active live slots — already entered, highest urgency
                    for ticker, slot in list(_ls.items()):
                        conf = float(slot.get("confidence", 0) or 0)
                        price = float(slot.get("price_cents") or slot.get("yes_ask") or 0)
                        # Skip if no real confidence or untradeable price (must be 5¢–95¢)
                        if conf < MIN_CONF or not (5 <= price <= 95):
                            continue
                        band = int(conf / 10) * 10
                        if _alerted.get(ticker, {}).get("band") != band:
                            pick = {**slot, "is_live": True, "ticker": ticker}
                            new_picks.append(pick)
                            _alerted[ticker] = {
                                "band": band, "pick": pick,
                                "alerted_at": now_utc, "result_sent": False,
                            }

                    # 2. BUY evaluations where the underlying event is LIVE RIGHT NOW
                    for ev in list(_da.all_evaluations):
                        if ev.get("action") != "BUY":
                            continue
                        conf   = float(ev.get("confidence", 0) or 0)
                        price  = float(ev.get("price_cents") or ev.get("yes_ask") or 0)
                        ticker = ev.get("ticker", "")
                        # Must have real confidence, tradeable price (5¢–95¢), and ticker
                        if conf < MIN_CONF or not (5 <= price <= 95) or not ticker:
                            continue
                        if ticker in _ls:
                            continue
                        title = ev.get("title", "") or ""
                        try:
                            live_now = await is_event_live_now(title)
                        except Exception:
                            live_now = False
                        if not live_now:
                            continue
                        band = int(conf / 10) * 10
                        if _alerted.get(ticker, {}).get("band") != band:
                            pick = {**ev, "is_live": True}
                            new_picks.append(pick)
                            _alerted[ticker] = {
                                "band": band, "pick": pick,
                                "alerted_at": now_utc, "result_sent": False,
                            }

                    if new_picks:
                        new_picks.sort(
                            key=lambda x: (0 if x.get("is_live") else 1, -x.get("confidence", 0))
                        )
                        # Build a fingerprint of this batch — ticker + confidence band
                        batch_keys = frozenset(
                            f"{p.get('ticker')}:{int(p.get('confidence',0)//10)*10}"
                            for p in new_picks
                        )
                        # Only fire if this is genuinely different from last alert
                        if batch_keys != _last_sent_keys:
                            await discord.bot_alert(new_picks[:6], mode=mode)
                            _last_sent_keys = batch_keys
                            logger.info("BOT ALERT fired: %d new picks", len(new_picks))
                        else:
                            logger.debug("BOT ALERT skipped — same picks as last send")

                    # Check results for previously alerted picks
                    await _check_and_post_results(discord, mode)

                except Exception as e:
                    logger.error("Bot alert loop error: %s", e)

        async def live_miss_scan_loop():
            """
            LIVE SCAN — runs every 5 minutes.

            Finds markets where a real-world event is happening RIGHT NOW,
            evaluates each with full context (web search + sports/crypto data),
            and records any the bot can't/won't trade as a live miss.

            Two scan types are tracked separately:
              LIVE     — events confirmed happening right now (SofaScore, CoinGecko, news)
              REGULAR  — everything else (handled by the main trade loop)

            Resolution checker runs every 5 min too: when a tracked market settles,
            records whether the bot's predicted side was correct.
            Hourly digest fires at the top of every hour with fresh correct misses.
            """
            from src.alerts.discord import DiscordAlerter
            from src.utils.live_miss_tracker import live_miss_tracker
            from src.data.live_event_detector import is_event_live_now
            from src.data.context_builder import build_market_context
            from src.ai.decision import AIDecisionEngine
            from src.utils.eastern_time import now_et
            from datetime import timedelta as _td

            LIVE_SCAN_INTERVAL  = 300   # 5 min
            _last_hour_digest   = None

            await asyncio.sleep(90)   # let bot warm up first
            logger.info("Live miss scan loop started — scanning every %ds", LIVE_SCAN_INTERVAL)

            # Morning wake-up summary at 5am ET after sleep mode
            _morning_summary_sent = False

            while not self._shutdown.is_set():
                await asyncio.sleep(LIVE_SCAN_INTERVAL)
                if self._shutdown.is_set():
                    break

                # Sleep mode 3–5am ET
                if await _sleep_mode_wait():
                    _morning_summary_sent = False  # reset so morning summary fires
                    continue

                # 5am morning wake-up summary
                et_now = now_et()
                if et_now.hour == 5 and not _morning_summary_sent:
                    try:
                        from src.alerts.discord import DiscordAlerter as _DA
                        _d = _DA()
                        open_pos = await self.db.fetchone("SELECT COUNT(*) as n FROM positions WHERE status='open'") or {}
                        unreal   = await self.db.fetchone("SELECT COALESCE(SUM(pnl),0) as p FROM positions WHERE status='open'") or {}
                        await _d.send_message(
                            f"☀️ **Good morning! Bot back online — 5:00 AM ET**\n"
                            f"📊 Open positions: **{open_pos.get('n', 0)}**\n"
                            f"💰 Unrealised PnL: **${float(unreal.get('p', 0) or 0):.2f}**\n"
                            f"🔍 Resuming live scan + trading now."
                        )
                        _morning_summary_sent = True
                        logger.info("Morning wake-up summary sent")
                    except Exception as _me:
                        logger.debug("Morning summary error: %s", _me)

                try:
                    mode = "PAPER" if not settings.trading.live_trading_enabled else "LIVE"

                    # ── 1. RESOLUTION CHECK — did any tracked miss resolve? ─────
                    pending = live_miss_tracker.pending_resolution()
                    for entry in pending:
                        ticker = entry.get("ticker", "")
                        if not ticker:
                            continue
                        try:
                            # Check DB: did this market close?
                            row = await self.db.fetchone(
                                "SELECT status, last_price, yes_ask, no_ask "
                                "FROM markets WHERE ticker=? LIMIT 1",
                                (ticker,)
                            )
                            if not row:
                                continue
                            status   = row.get("status", "")
                            yes_ask  = float(row.get("yes_ask") or row.get("last_price") or 0)
                            # A market is resolved when yes_ask hits near 0 or near 100
                            if status == "closed" or yes_ask <= 3 or yes_ask >= 97:
                                actual = "yes" if yes_ask >= 97 else "no" if yes_ask <= 3 else None
                                if actual:
                                    live_miss_tracker.mark_resolved(ticker, actual)
                        except Exception:
                            pass

                    # ── 2. LIVE EVENT SCAN — ALL categories + sub-categories ──────
                    # Pull every open market from DB across all categories.
                    # Price normalised: stored as cents (45) or decimal (0.45) both work.
                    try:
                        candidates = await self.db.fetchall(
                            "SELECT ticker, title, yes_ask, no_ask, last_price, volume, "
                            "platform, close_time, category FROM markets "
                            "WHERE (status='open' OR status='') "
                            "AND (yes_ask > 0 OR last_price > 0) "
                            "AND title IS NOT NULL AND title != '' "
                            "ORDER BY volume DESC, close_time ASC "
                            "LIMIT 2000"
                        ) or []
                        # Normalise prices so downstream code always sees cents format
                        normed = []
                        for r in candidates:
                            m = dict(r)
                            def _n(v):
                                try:
                                    f = float(v or 0)
                                    return f if f <= 1.0 else f / 100.0
                                except Exception:
                                    return 0.0
                            ya = _n(m.get("yes_ask") or m.get("last_price"))
                            if 0.03 <= ya <= 0.97:   # 3¢–97¢ tradeable range
                                m["yes_ask"] = round(ya * 100, 2)
                                m["no_ask"]  = round((1 - ya) * 100, 2)
                                normed.append(m)
                        candidates = normed
                    except Exception as _ce:
                        logger.debug("Live scan DB query failed: %s", _ce)
                        candidates = []

                    # Also pull live-now markets directly from both platforms
                    try:
                        from src.clients.kalshi_client import KalshiClient as _KC
                        from src.clients.polymarket_client import PolymarketTradingClient as _PC
                        _kc = _KC()
                        _pc = _PC()
                        kalshi_live_now, poly_live_now = await asyncio.gather(
                            _kc.get_live_now_markets(max_markets=200),
                            _pc.get_live_now_markets(max_markets=200),
                            return_exceptions=True,
                        )
                        for m in (kalshi_live_now if isinstance(kalshi_live_now, list) else []):
                            m["_platform_live"] = True
                            candidates.append(m)
                        for m in (poly_live_now if isinstance(poly_live_now, list) else []):
                            m["_platform_live"] = True
                            candidates.append(m)
                    except Exception as _pe:
                        logger.debug("Live platform fetch in scan loop: %s", _pe)

                    # Deduplicate by ticker
                    seen_t: set = set()
                    deduped = []
                    for m in candidates:
                        t = m.get("ticker") or m.get("condition_id") or ""
                        if t and t not in seen_t:
                            deduped.append(m)
                            seen_t.add(t)
                    candidates = deduped

                    # Filter to markets where the underlying event is live RIGHT NOW
                    already_tracked  = {t for t in live_miss_tracker._misses}
                    already_position = set()
                    try:
                        pos_rows = await self.db.fetchall(
                            "SELECT ticker FROM positions WHERE status='open'"
                        ) or []
                        already_position = {r["ticker"] for r in pos_rows}
                    except Exception:
                        pass

                    # Run is_event_live_now() in parallel for all candidates
                    # Skip check for markets already confirmed live by platform API
                    live_candidates = []
                    to_check = []
                    for m in candidates:
                        ticker = m.get("ticker", "") or m.get("condition_id", "")
                        title  = m.get("title", "") or ""
                        if not ticker or not title:
                            continue
                        if ticker in already_position:
                            continue
                        if m.get("_platform_live"):
                            # Already confirmed live by Kalshi/Poly native API
                            live_candidates.append(dict(m))
                        else:
                            to_check.append(dict(m))

                    # Parallel live check on remaining candidates
                    if to_check:
                        try:
                            check_results = await asyncio.gather(
                                *[is_event_live_now(m.get("title", "")) for m in to_check],
                                return_exceptions=True,
                            )
                            for m, result in zip(to_check, check_results):
                                if result is True:
                                    live_candidates.append(m)
                        except Exception as _lce:
                            logger.debug("Parallel live check error: %s", _lce)

                    if not live_candidates:
                        # Still run hourly digest even if no new live markets
                        pass
                    else:
                        logger.info(
                            "Live miss scan: %d live-event markets found out of %d candidates",
                            len(live_candidates), len(candidates),
                        )

                    # ── 3. AI-EVALUATE live candidates — find cheeky bids ─────
                    engine = AIDecisionEngine(db=self.db)
                    for m in live_candidates[:20]:  # up to 20 live markets per 5-min cycle
                        ticker = m.get("ticker", "")
                        title  = m.get("title", "")

                        # Skip if already tracked with fresh data (< 10 min ago)
                        existing = live_miss_tracker._misses.get(ticker)
                        if existing and not existing.get("resolved_at"):
                            try:
                                scanned = datetime.fromisoformat(existing["scanned_at"])
                                if scanned.tzinfo is None:
                                    scanned = scanned.replace(tzinfo=timezone.utc)
                                if (datetime.now(timezone.utc) - scanned).total_seconds() < 600:
                                    continue   # re-evaluated within last 10 min, skip
                            except Exception:
                                pass

                        try:
                            # Build rich context for this live market
                            m["is_live"] = True
                            context = await build_market_context(m, timeout_seconds=12.0)
                            decision = await engine.decide(m, signals=[])

                            action = decision.action
                            conf   = decision.confidence
                            side   = decision.side or "yes"
                            ev     = decision.net_ev

                            # Determine skip reason
                            if ticker in already_position:
                                skip_reason = "already in position"
                            elif action == "HOLD":
                                skip_reason = f"AI said HOLD (conf={conf:.0f}%)"
                            elif conf < settings.trading.min_ai_confidence:
                                skip_reason = f"conf {conf:.0f}% < {settings.trading.min_ai_confidence:.0f}% required"
                            elif ev is not None and ev <= 0:
                                skip_reason = f"EV {ev:+.1f}¢ not positive"
                            else:
                                # Bot COULD have traded but daily limit / risk gate blocked it
                                skip_reason = "daily trade limit or risk gate"

                            # Record as live miss regardless of outcome
                            # (we'll know if it was correct when it resolves)
                            live_miss_tracker.record(
                                ticker      = ticker,
                                title       = title,
                                side        = side,
                                confidence  = conf,
                                yes_ask     = float(m.get("yes_ask", 0)),
                                no_ask      = float(m.get("no_ask", 0)),
                                reasoning   = decision.reasoning[:200],
                                skip_reason = skip_reason,
                                scan_type   = "live",
                                net_ev      = ev,
                                true_prob   = decision.true_prob,
                                platform    = m.get("platform", "kalshi"),
                            )
                            logger.debug(
                                "LiveMiss tracked: %s | %s/%s | conf=%.0f%% | %s",
                                ticker[:35], action, side.upper(), conf, skip_reason[:40],
                            )
                        except Exception as _ev_err:
                            logger.debug("LiveMiss eval error %s: %s", ticker[:30], _ev_err)

                    # ── 4. HOURLY MISS DIGEST — top of every hour ─────────────
                    et_now   = now_et()
                    this_hour = et_now.replace(minute=0, second=0, microsecond=0)
                    if _last_hour_digest != this_hour:
                        _last_hour_digest = this_hour
                        try:
                            discord = DiscordAlerter()
                            await discord.live_miss_digest(
                                paper=not settings.trading.live_trading_enabled
                            )
                        except Exception as _de:
                            logger.debug("Live miss digest error: %s", _de)

                except Exception as e:
                    logger.error("Live miss scan loop error: %s", e)

        tasks = [
            asyncio.create_task(ingest_loop(),                name="ingest"),
            asyncio.create_task(track_loop(),                 name="track"),
            asyncio.create_task(trade_loop(),                 name="trade"),
            asyncio.create_task(hourly_heartbeat_loop(),      name="hourly_heartbeat"),
            asyncio.create_task(daytime_summary_loop(),       name="daytime_summary"),
            asyncio.create_task(daily_summary_loop(),         name="daily_summary"),
            asyncio.create_task(bot_alert_loop(),             name="bot_alert"),
            asyncio.create_task(live_miss_scan_loop(),        name="live_miss_scan"),
            # asyncio.create_task(discord_command_loop(),      name="discord_commands"),  # enable when Discord bot token is set up
            asyncio.create_task(live_market_manager_loop(),    name="live_market_manager"),
            asyncio.create_task(manual_trade_monitor_loop(),  name="manual_trade_monitor"),
        ]

        await self._shutdown.wait()
        logger.info("Cancelling background tasks...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Bot stopped cleanly.")


async def main():
    parser = argparse.ArgumentParser(description="Kalshi AI Trading Bot")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (default: paper)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle then exit (for testing)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    setup_logging(args.log_level)

    bot = TradingBot(live_mode=args.live)

    if args.once:
        await bot.startup()
        await bot.run_cycle()
        # run_evaluation removed — single cycle complete
        logger.info("--once complete.")
    else:
        await bot.run_loop()


if __name__ == "__main__":
    asyncio.run(main())
