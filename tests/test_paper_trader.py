"""Tests for PaperTrader and paper signal tracker."""

import os
import pytest
import pytest_asyncio
from src.utils.database import DatabaseManager
from src.execution.paper_trader import PaperTrader
from src.paper.tracker import log_signal, settle_signal, get_all_signals, get_stats
from src.paper.dashboard import generate_html

DB_PATH = "test_paper_unit.db"


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


@pytest.fixture
async def db():
    d = DatabaseManager(DB_PATH)
    await d.initialize()
    return d


@pytest.mark.asyncio
async def test_paper_trade_executes(db):
    trader = PaperTrader(db=db)
    rec = await trader.execute("FAKE-1", "BUY", "yes", 45.0, ai_confidence=75.0)
    assert rec is not None
    assert rec["ticker"] == "FAKE-1"
    assert rec["contracts"] > 0
    assert rec["paper_trade"] == 1


@pytest.mark.asyncio
async def test_paper_trade_skips_zero_price(db):
    trader = PaperTrader(db=db)
    rec = await trader.execute("FAKE-2", "BUY", "yes", 0.0)
    assert rec is None


@pytest.mark.asyncio
async def test_stats_after_trade(db):
    trader = PaperTrader(db=db)
    await trader.execute("FAKE-3", "BUY", "yes", 50.0, ai_confidence=80.0)
    stats = await trader.get_stats()
    assert stats["total_trades"] == 1


@pytest.mark.asyncio
async def test_trade_history(db):
    trader = PaperTrader(db=db)
    await trader.execute("FAKE-4", "BUY", "yes", 50.0)
    history = await trader.get_history()
    assert len(history) == 1
    assert history[0]["ticker"] == "FAKE-4"


@pytest.mark.asyncio
async def test_log_and_settle_signal(db):
    sid = await log_signal(db, "SIG-1", "BUY", "yes", 45.0, 2, 80.0, "test", 0, "unit_test")
    assert sid >= 1
    await settle_signal(db, sid, 1.10)
    signals = await get_all_signals(db)
    settled = [s for s in signals if s["id"] == sid][0]
    assert settled["settled"] == 1
    assert settled["outcome"] == 1.10


@pytest.mark.asyncio
async def test_stats_win_rate(db):
    await log_signal(db, "W1", "BUY", "yes", 45.0, 1, 75.0, "", 0, "test")
    await log_signal(db, "W2", "BUY", "yes", 45.0, 1, 75.0, "", 0, "test")
    sid1 = (await get_all_signals(db))[1]["id"]
    sid2 = (await get_all_signals(db))[0]["id"]
    await settle_signal(db, sid1, 1.0)
    await settle_signal(db, sid2, -0.5)
    stats = await get_stats(db)
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50.0


def test_dashboard_html_generates():
    signals = [{"ticker": "A", "action": "BUY", "side": "yes", "price": 45,
                "ai_confidence": 75, "outcome": 1.0, "created_at": "2026-01-01T00:00:00"}]
    stats = {"total_pnl": 1.0, "win_rate": 100.0, "total": 1}
    html = generate_html(signals, stats)
    assert "FAKE" not in html
    assert "1.00" in html  # pnl shown
    assert len(html) > 500
