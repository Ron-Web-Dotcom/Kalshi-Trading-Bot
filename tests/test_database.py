"""Tests for DatabaseManager."""

import asyncio
import os
import pytest
import pytest_asyncio
from src.utils.database import DatabaseManager

DB_PATH = "test_db_unit.db"


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


@pytest.mark.asyncio
async def test_initialize_creates_tables():
    db = DatabaseManager(DB_PATH)
    await db.initialize()
    # Should be idempotent
    await db.initialize()
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = {r["name"] for r in rows}
    for expected in ("markets", "positions", "trade_logs", "paper_signals",
                     "ai_decisions", "performance_metrics", "daily_stats"):
        assert expected in table_names, f"Missing table: {expected}"


@pytest.mark.asyncio
async def test_insert_and_fetchone():
    db = DatabaseManager(DB_PATH)
    await db.initialize()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    rid = await db.insert("paper_signals", {
        "ticker": "TEST-1",
        "action": "BUY",
        "side": "yes",
        "price": 45.0,
        "contracts": 2,
        "ai_confidence": 80.0,
        "ai_reasoning": "test",
        "arbitrage_pct": 0,
        "signal_source": "unit_test",
        "outcome": None,
        "settled": 0,
        "created_at": now,
    })
    assert rid >= 1
    row = await db.fetchone("SELECT * FROM paper_signals WHERE id=?", (rid,))
    assert row is not None
    assert row["ticker"] == "TEST-1"
    assert row["action"] == "BUY"


@pytest.mark.asyncio
async def test_execute_update():
    db = DatabaseManager(DB_PATH)
    await db.initialize()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    rid = await db.insert("paper_signals", {
        "ticker": "TEST-2", "action": "BUY", "side": "yes",
        "price": 50.0, "contracts": 1, "ai_confidence": 70.0,
        "ai_reasoning": "", "arbitrage_pct": 0, "signal_source": "test",
        "outcome": None, "settled": 0, "created_at": now,
    })
    await db.execute("UPDATE paper_signals SET settled=1, outcome=1.0 WHERE id=?", (rid,))
    row = await db.fetchone("SELECT * FROM paper_signals WHERE id=?", (rid,))
    assert row["settled"] == 1
    assert row["outcome"] == 1.0


@pytest.mark.asyncio
async def test_get_open_positions_empty():
    db = DatabaseManager(DB_PATH)
    await db.initialize()
    positions = await db.get_open_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_daily_ai_cost_zero():
    db = DatabaseManager(DB_PATH)
    await db.initialize()
    cost = await db.get_daily_ai_cost()
    assert cost == 0.0
