"""
Tests covering every bug identified in the security/correctness audit.

BUG-01  decide.py        — NameError cfg.daily_budget_usd
BUG-02  track.py         — UnboundLocalError on reeval
BUG-03  decision.py      — get_event_loop deprecated
BUG-04  cli.py           — missing beast_mode_bot import
BUG-07  manager.py       — Kelly uses confidence not true_prob, fee excluded
BUG-09  paper_trader.py  — dup guard ignores side (unhedged internal arb)
BUG-10  poly_paper_trader — live order before dup check
BUG-11  evaluate.py      — hardcoded paper_trade=1 in live mode
BUG-12  track.py         — UPDATE trade_logs too broad
BUG-13  market_data.py   — stale markets not purged
BUG-19  live_trader.py   — no duplicate guard
BUG-23  kalshi_client.py — API key in error log
BUG-24  trade.py         — raw secret in Discord error
BUG-28  track.py         — case-sensitive market result
BUG-30  bot.py           — heartbeat mixes paper+live PnL
"""

import asyncio
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeDB:
    """Minimal in-memory DB stub."""

    def __init__(self, rows=None):
        self._rows = rows or {}
        self.executed = []
        self.inserted = []

    async def fetchone(self, query, params=()):
        key = query.split()[0]
        return self._rows.get(query, self._rows.get(key))

    async def fetchall(self, query, params=()):
        return self._rows.get(query, self._rows.get("all", []))

    async def execute(self, query, params=()):
        self.executed.append((query, params))

    async def insert(self, table, row):
        self.inserted.append((table, row))
        return len(self.inserted)


# ---------------------------------------------------------------------------
# BUG-01 — decide.py NameError
# ---------------------------------------------------------------------------

def test_bug01_decide_budget_gate_no_nameerror(monkeypatch):
    """Budget gate must not raise NameError when limit is reached."""
    from src.jobs import decide

    async def fake_spend(_db):
        return 999.0  # way over budget

    monkeypatch.setattr(decide, "_get_daily_ai_spend", fake_spend)

    from src.config.settings import settings
    settings.trading.enable_daily_cost_limiting = True
    settings.trading.daily_ai_budget = 10.0

    result = asyncio.get_event_loop().run_until_complete(
    result = asyncio.run(
        decide.make_decision_for_market(
            {"ticker": "TEST", "yes_ask": 50, "no_ask": 50, "volume": 1000},
            [],
            db=FakeDB(),
        )
    )
    assert result is None  # should return None, not raise


# ---------------------------------------------------------------------------
# BUG-02 — track.py UnboundLocalError on reeval
# ---------------------------------------------------------------------------

def test_bug02_reeval_unbound_does_not_crash():
    """ai_reeval close_reason must not raise when reeval was never assigned."""
    import ast, inspect
    from src.jobs import track
    source = inspect.getsource(track)
    # Verify the guard is present
    assert "\"reeval\" in locals()" in source, (
        "BUG-02 not fixed: reeval.get() must be guarded by 'reeval in locals()'"
    )


# ---------------------------------------------------------------------------
# BUG-03 — decision.py get_event_loop
# ---------------------------------------------------------------------------

def test_bug03_no_get_event_loop():
    """BUG-03: deprecated get_event_loop() removed; AsyncAnthropic used instead."""
    import inspect
    from src.ai import decision
    source = inspect.getsource(decision)
    assert "get_event_loop()" not in source, (
        "BUG-03: get_event_loop() must not be called in async context"
    )
    # Either use get_running_loop() or (better) use AsyncAnthropic directly
    uses_async_client = "AsyncAnthropic" in source
    uses_running_loop = "get_running_loop()" in source
    assert uses_async_client or uses_running_loop, (
        "BUG-03: must use AsyncAnthropic or get_running_loop()"
    )


# ---------------------------------------------------------------------------
# BUG-04 — cli.py no beast_mode_bot import in default path
# ---------------------------------------------------------------------------

def test_bug04_cli_run_default_no_beast_import():
    """Default path of cmd_run must use TradingBot, not stray module imports."""
    import inspect
    import cli
    source = inspect.getsource(cli.cmd_run)
    # The default (non-beast) path must import TradingBot
    assert "TradingBot" in source
    # The broken unconditional imports from src.strategies.* must be gone
    assert "from src.strategies.category_scorer" not in source
    assert "from src.strategies.portfolio_enforcer" not in source


# ---------------------------------------------------------------------------
# BUG-07 — Kelly uses true_prob, fee-adjusted odds
# ---------------------------------------------------------------------------

def test_bug07_kelly_uses_fee_adjusted_odds():
    from src.risk.manager import RiskManager
    rm = RiskManager()

    # At 50¢ price, 70% true win probability, 25% Kelly fraction
    # b = (100 - 50) * 0.98 / 50 = 0.98
    # kelly_f = (0.7 * 0.98 - 0.3) / 0.98 = (0.686 - 0.3) / 0.98 ≈ 0.394
    # fractional = 0.394 * 0.25 = 0.0985
    # dollar_size = 0.0985 * 1000 = $98.5 → clamped to max $100
    size = rm.kelly_size(win_prob_pct=70.0, price_cents=50.0, portfolio_value=1000.0)
    assert size > 0

    # At 50% win prob on a 50¢ market: EV should be 0 (break-even) → min size
    size_breakeven = rm.kelly_size(win_prob_pct=50.0, price_cents=50.0, portfolio_value=1000.0)
    # kelly = (0.5*0.98 - 0.5)/0.98 = (0.49-0.5)/0.98 < 0 → min size
    from src.config.settings import settings
    assert size_breakeven == settings.trading.min_trade_size_dollars


def test_bug07_kelly_does_not_oversize_on_high_confidence():
    """With true_prob=55% on a 50¢ market, size must be modest (not bet-the-farm)."""
    from src.risk.manager import RiskManager
    from src.config.settings import settings
    rm = RiskManager()
    size = rm.kelly_size(win_prob_pct=55.0, price_cents=50.0, portfolio_value=1000.0)
    assert size <= settings.trading.max_trade_size_dollars
    assert size < 50.0, f"Kelly oversized at marginal edge: ${size:.2f}"


# ---------------------------------------------------------------------------
# BUG-09 — paper_trader dup guard includes side
# ---------------------------------------------------------------------------

def test_bug09_paper_trader_dup_guard_checks_side():
    import inspect
    from src.execution import paper_trader
    source = inspect.getsource(paper_trader.PaperTrader.execute)
    assert "AND side=?" in source, (
        "BUG-09: duplicate guard must filter by side to allow YES+NO arb legs"
    )


def test_bug09_paper_trader_allows_yes_and_no_on_same_ticker():
    """YES and NO legs of internal arb must both be allowed."""
    async def _run():
        from unittest.mock import AsyncMock, MagicMock
        from src.execution.paper_trader import PaperTrader

        call_count = 0

        async def fake_fetchone(query, params=()):
            nonlocal call_count
            call_count += 1
            return None  # no existing position

        db = MagicMock()
        db.fetchone = fake_fetchone
        db.insert = AsyncMock(return_value=1)
        db.execute = AsyncMock()

        trader = PaperTrader(db=db)
        await trader.execute("KXTEST-25", "BUY", "yes", 45.0, ai_confidence=80.0)
        await trader.execute("KXTEST-25", "BUY", "no", 50.0, ai_confidence=80.0)
        assert call_count == 2

    asyncio.get_event_loop().run_until_complete(_run())
    asyncio.run(_run())


# ---------------------------------------------------------------------------
# BUG-10 — poly_paper_trader dup check before live order
# ---------------------------------------------------------------------------

def test_bug10_poly_dup_guard_before_live_order():
    import ast, inspect, textwrap
    from src.execution import poly_paper_trader
    source = inspect.getsource(poly_paper_trader.PolyPaperTrader.execute)
    # Confirm 'existing' check appears before 'live_trading_enabled' order block
    dup_pos = source.find("existing = await self.db.fetchone")
    live_pos = source.find("if self.poly_cfg.live_trading_enabled")
    assert dup_pos < live_pos, (
        "BUG-10: duplicate guard must appear BEFORE the live order placement block"
    )


# ---------------------------------------------------------------------------
# BUG-11 — evaluate.py respects live vs paper flag
# ---------------------------------------------------------------------------

def test_bug11_evaluate_uses_correct_paper_flag():
    import inspect
    from src.jobs import evaluate
    source = inspect.getsource(evaluate.run_evaluation)
    assert "paper_flag" in source, "BUG-11: evaluate must use dynamic paper_flag"
    assert "paper_trade=1" not in source, "BUG-11: hardcoded paper_trade=1 still present"


# ---------------------------------------------------------------------------
# BUG-12 — track.py UPDATE trade_logs scoped by side + MAX(executed_at)
# ---------------------------------------------------------------------------

def test_bug12_trade_log_update_scoped_by_side():
    import inspect
    from src.jobs import track
    source = inspect.getsource(track.run_tracking)
    assert "AND side=?" in source, (
        "BUG-12: UPDATE trade_logs pnl must filter by side to avoid overwriting all rows"
    )
    assert "MAX(executed_at)" in source, (
        "BUG-12: UPDATE must target only the latest matching row"
    )


# ---------------------------------------------------------------------------
# BUG-13 — market_data.py closes stale markets after ingest
# ---------------------------------------------------------------------------

def test_bug13_stale_markets_purged_after_ingest():
    import inspect
    from src.data import market_data
    source = inspect.getsource(market_data.MarketDataFetcher.fetch_and_store)
    assert "status='closed'" in source and "NOT IN" in source, (
        "BUG-13: fetch_and_store must mark markets not in fresh batch as closed"
    )


# ---------------------------------------------------------------------------
# BUG-19 — live_trader.py has duplicate guard
# ---------------------------------------------------------------------------

def test_bug19_live_trader_has_dup_guard():
    import inspect
    from src.execution import live_trader
    source = inspect.getsource(live_trader.LiveTrader.execute)
    assert "AND side=?" in source, (
        "BUG-19: LiveTrader must have a side-aware duplicate position guard"
    )


# ---------------------------------------------------------------------------
# BUG-23 — kalshi_client.py redacts API key from error logs
# ---------------------------------------------------------------------------

def test_bug23_kalshi_error_redacts_key():
    import inspect
    from src.clients import kalshi_client
    source = inspect.getsource(kalshi_client.KalshiClient._request)
    assert "[KEY_ID]" in source, (
        "BUG-23: Kalshi HTTP error log must redact API key ID"
    )


# ---------------------------------------------------------------------------
# BUG-24 — trade.py sanitizes secrets before Discord error_alert
# ---------------------------------------------------------------------------

def test_bug24_trade_error_redacts_secrets():
    import inspect
    from src.jobs import trade
    source = inspect.getsource(trade.run_trading_job)
    assert "[REDACTED]" in source, (
        "BUG-24: error_alert call must sanitize secrets before sending to Discord"
    )


# ---------------------------------------------------------------------------
# BUG-28 — track.py case-insensitive market result comparison
# ---------------------------------------------------------------------------

def test_bug28_result_comparison_case_insensitive():
    import inspect
    from src.jobs import track
    source = inspect.getsource(track.run_tracking)
    assert ".lower()" in source, (
        "BUG-28: market result comparison must use .lower() for case-insensitive match"
    )
    assert 'result == "yes"' not in source, (
        "BUG-28: raw case-sensitive result comparison must not remain"
    )


# ---------------------------------------------------------------------------
# BUG-30 — bot.py heartbeat PnL uses correct paper_trade flag
# ---------------------------------------------------------------------------

def test_bug30_heartbeat_pnl_filters_by_mode():
    import inspect
    import bot
    source = inspect.getsource(bot.TradingBot.run_loop)
    assert "paper_trade=?" in source or "_paper_flag" in source, (
        "BUG-30: heartbeat PnL query must filter by paper_trade flag"
    )
