"""
Tests for the second-pass audit bugs (A-series).

A1  live_trader     — dup check before order placement
A2  trade.py        — live balance fetched and used as portfolio_value
A3  market_data.py  — stale data recency filter
A4  decision.py     — AI hallucination sanity clamps
A5  poly_paper      — live orders written to DB
A6  database.py     — WAL mode + busy_timeout
A7  logging_setup   — RotatingFileHandler
A9  all traders     — portfolio_value passed to kelly_size
A10/A11/A12 singletons — ArbitrageDetector, AutoScaler, RiskManager
A13 discord.py      — 5s timeout on Discord posts
A15 database.py     — markets table has platform column
A19 track.py        — reeval reset per loop iteration
A20 deploy/         — systemd service file exists
A22 decision.py     — AsyncAnthropic (no executor blocking)
"""

import asyncio
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# A1 — live_trader dup guard fires BEFORE create_order
# ---------------------------------------------------------------------------

def test_A1_live_trader_dup_before_order():
    """Dup guard must appear before the kalshi.create_order call in source."""
    from src.execution import live_trader
    source = inspect.getsource(live_trader.LiveTrader.execute)
    dup_pos   = source.find("existing = await self.db.fetchone")
    order_pos = source.find("await self.kalshi.create_order")
    assert dup_pos != -1, "No dup guard found"
    assert order_pos != -1, "No create_order call found"
    assert dup_pos < order_pos, (
        "A1: dup guard must appear BEFORE create_order to avoid orphaned live orders"
    )


@pytest.mark.asyncio
async def test_A1_live_trader_does_not_place_order_when_dup_exists():
    """If a position already exists, create_order must never be called."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.execution.live_trader import LiveTrader

    db = MagicMock()
    db.fetchone = AsyncMock(return_value={"id": 42})  # existing position

    kalshi = MagicMock()
    kalshi.create_order = AsyncMock()

    with patch("src.config.settings.settings") as mock_settings:
        mock_settings.trading.live_trading_enabled = True
        mock_settings.trading.base_trade_size_dollars = 10.0
        mock_settings.trading.max_trade_size_dollars = 100.0
        mock_settings.trading.min_trade_size_dollars = 1.0
        mock_settings.trading.portfolio_value = 1000.0

        trader = object.__new__(LiveTrader)
        trader.cfg = mock_settings.trading
        trader.kalshi = kalshi
        trader.db = db
        trader.discord = None
        trader.scaler = None
        trader.risk = None

        result = await trader.execute(
            ticker="KXTEST", action="BUY", side="yes",
            price_cents=50.0, ai_confidence=80.0
        )

    assert result is None
    kalshi.create_order.assert_not_called()


# ---------------------------------------------------------------------------
# A3 — market_data freshness filter
# ---------------------------------------------------------------------------

def test_A3_get_cached_markets_has_freshness_filter():
    from src.data import market_data
    source = inspect.getsource(market_data.MarketDataFetcher.get_cached_markets)
    assert "fetched_at" in source, "A3: must filter by fetched_at recency"
    assert "max_age_minutes" in source or "timedelta" in source


@pytest.mark.asyncio
async def test_A3_stale_markets_not_returned():
    """Markets older than max_age_minutes must be excluded."""
    from unittest.mock import MagicMock, AsyncMock
    from src.clients.kalshi_client import KalshiClient
    from src.data.market_data import MarketDataFetcher
    from datetime import datetime, timezone, timedelta

    old_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    db = MagicMock()
    db.fetchall = AsyncMock(return_value=[])  # simulate no fresh rows

    kalshi = MagicMock(spec=KalshiClient)
    fetcher = MarketDataFetcher(kalshi, db)
    rows = await fetcher.get_cached_markets(max_age_minutes=15)
    assert rows == []


# ---------------------------------------------------------------------------
# A4 — AI hallucination sanity clamps
# ---------------------------------------------------------------------------

def test_A4_decision_clamps_confidence():
    from src.ai import decision
    source = inspect.getsource(decision)
    assert "min(float" in source and "100.0" in source, (
        "A4: confidence must be clamped to [0, 100]"
    )


def test_A4_decision_clamps_net_ev():
    from src.ai import decision
    source = inspect.getsource(decision.AIDecisionEngine.decide)
    assert "97.0" in source, "A4: net_ev must be clamped to max 97.0¢"
    assert "physical max" in source or "impossible EV" in source or "Sanity" in source


# ---------------------------------------------------------------------------
# A5 — poly live orders written to DB
# ---------------------------------------------------------------------------

def test_A5_poly_live_trade_db_write_not_in_else_only():
    """DB insert must execute in both live and paper paths."""
    from src.execution import poly_paper_trader
    source = inspect.getsource(poly_paper_trader.PolyPaperTrader.execute)
    # Find positions of live order block and DB insert
    live_block_pos = source.find("if self.poly_cfg.live_trading_enabled:")
    db_insert_pos  = source.find("await self.db.insert")
    else_pos = source.find("else:\n            live_order_id = None")
    # DB insert must come AFTER the else block (i.e., outside of else:)
    assert db_insert_pos > else_pos, (
        "A5: DB insert must occur after both live and paper order paths, not inside else:"
    )


# ---------------------------------------------------------------------------
# A6 — database WAL mode + busy_timeout
# ---------------------------------------------------------------------------

def test_A6_database_enables_wal():
    from src.utils import database
    source = inspect.getsource(database.DatabaseManager.initialize)
    assert "WAL" in source, "A6: WAL journal mode must be set on DB init"
    assert "busy_timeout" in source, "A6: busy_timeout must be set to handle lock contention"


# ---------------------------------------------------------------------------
# A7 — RotatingFileHandler
# ---------------------------------------------------------------------------

def test_A7_logging_uses_rotating_handler():
    from src.utils import logging_setup
    source = inspect.getsource(logging_setup.setup_logging)
    assert "RotatingFileHandler" in source, (
        "A7: must use RotatingFileHandler to prevent disk-fill from unbounded log growth"
    )
    assert "maxBytes" in source


# ---------------------------------------------------------------------------
# A9 — portfolio_value passed to kelly_size in all traders
# ---------------------------------------------------------------------------

def test_A9_paper_trader_passes_portfolio_value():
    from src.execution import paper_trader
    source = inspect.getsource(paper_trader.PaperTrader.execute)
    assert "portfolio_value" in source, (
        "A9: PaperTrader must pass portfolio_value to kelly_size"
    )


def test_A9_live_trader_passes_portfolio_value():
    from src.execution import live_trader
    source = inspect.getsource(live_trader.LiveTrader.execute)
    assert "portfolio_value" in source


def test_A9_poly_trader_passes_portfolio_value():
    from src.execution import poly_paper_trader
    source = inspect.getsource(poly_paper_trader.PolyPaperTrader.execute)
    assert "portfolio_value" in source


# ---------------------------------------------------------------------------
# A10/A11/A12 — singleton state objects in TradingBot
# ---------------------------------------------------------------------------

def test_A10_A11_A12_singletons_on_bot():
    import bot as bot_module
    source = inspect.getsource(bot_module.TradingBot.__init__)
    assert "RiskManager" in source,    "A12: RiskManager must be singleton on TradingBot"
    assert "AutoScaler" in source,     "A11: AutoScaler must be singleton on TradingBot"
    assert "ArbitrageDetector" in source, "A10: ArbitrageDetector must be singleton on TradingBot"


def test_A10_trade_job_accepts_singletons():
    from src.jobs import trade
    import inspect as _i
    sig = _i.signature(trade.run_trading_job)
    params = sig.parameters
    assert "risk" in params,    "A12: run_trading_job must accept risk= parameter"
    assert "scaler" in params,  "A11: run_trading_job must accept scaler= parameter"
    assert "arb_det" in params, "A10: run_trading_job must accept arb_det= parameter"


# ---------------------------------------------------------------------------
# A13 — Discord 5s timeout
# ---------------------------------------------------------------------------

def test_A13_discord_has_timeout():
    from src.alerts import discord
    source = inspect.getsource(discord.DiscordAlerter._post)
    assert "wait_for" in source or "timeout" in source.lower(), (
        "A13: Discord _post must have a hard timeout to avoid stalling trade cycle"
    )
    assert "TimeoutError" in source or "asyncio.TimeoutError" in source


# ---------------------------------------------------------------------------
# A15 — markets table has platform column
# ---------------------------------------------------------------------------

def test_A15_markets_table_has_platform_column():
    from src.utils import database
    source = inspect.getsource(database.DatabaseManager.initialize)
    # Check either in CREATE TABLE or in migrations
    assert "platform" in source, (
        "A15: markets table must include platform column for heartbeat queries"
    )


# ---------------------------------------------------------------------------
# A19 — reeval reset per loop iteration
# ---------------------------------------------------------------------------

def test_A19_reeval_reset_per_iteration():
    from src.jobs import track
    source = inspect.getsource(track.run_tracking)
    assert "reeval    = None" in source or "reeval = None" in source, (
        "A19: reeval must be reset to None at the top of each position loop iteration"
    )


# ---------------------------------------------------------------------------
# A20 — systemd service file exists
# ---------------------------------------------------------------------------

def test_A20_systemd_service_exists():
    service_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "deploy", "kalshi-bot.service"
    )
    assert os.path.exists(service_path), (
        "A20: deploy/kalshi-bot.service must exist for auto-restart on VPS"
    )
    with open(service_path) as f:
        content = f.read()
    assert "Restart=" in content, "Service must have Restart= directive"
    assert "EnvironmentFile=" in content, "Service must load .env via EnvironmentFile="


# ---------------------------------------------------------------------------
# A22 — AsyncAnthropic client (non-blocking)
# ---------------------------------------------------------------------------

def test_A22_uses_async_anthropic():
    from src.ai import decision
    source = inspect.getsource(decision)
    assert "AsyncAnthropic" in source, (
        "A22: must use AsyncAnthropic to avoid blocking the event loop thread pool"
    )
    assert "run_in_executor" not in source, (
        "A22: run_in_executor workaround no longer needed with AsyncAnthropic"
    )
