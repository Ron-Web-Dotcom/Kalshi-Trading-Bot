"""Tests for AI decision engine (rule-based fallback path, no API key needed)."""

import os
import pytest

os.environ.setdefault("OPENAI_API_KEY", "")  # ensure no key → rule-based path

from src.ai.decision import AIDecisionEngine


@pytest.fixture
def engine():
    return AIDecisionEngine(db=None)


@pytest.mark.asyncio
async def test_hold_on_low_volume(engine):
    market = {"ticker": "T1", "title": "Test", "yes_ask": 45, "no_ask": 55, "volume": 10}
    decision = await engine.decide(market, [])
    assert decision.action == "HOLD"
    assert decision.ticker == "T1"


@pytest.mark.asyncio
async def test_hold_on_extreme_price(engine):
    market = {"ticker": "T2", "title": "Test", "yes_ask": 2, "no_ask": 98, "volume": 500}
    decision = await engine.decide(market, [])
    assert decision.action == "HOLD"


@pytest.mark.asyncio
async def test_buy_on_arb_signal(engine):
    market = {"ticker": "T3", "title": "Test", "yes_ask": 45, "no_ask": 55, "volume": 500}
    signals = [{"ticker": "T3", "diff_pct": 8.0, "edge_cents": 8, "signal_source": "arbitrage"}]
    decision = await engine.decide(market, signals)
    assert decision.action == "BUY"
    assert decision.confidence >= 65.0


@pytest.mark.asyncio
async def test_should_trade_above_threshold(engine):
    from src.ai.decision import AIDecision
    d = AIDecision(action="BUY", confidence=80.0, reasoning="", model="test", ticker="X")
    assert engine.should_trade(d) is True


@pytest.mark.asyncio
async def test_should_not_trade_below_threshold(engine):
    from src.ai.decision import AIDecision
    d = AIDecision(action="BUY", confidence=50.0, reasoning="", model="test", ticker="X")
    assert engine.should_trade(d) is False


@pytest.mark.asyncio
async def test_hold_never_trades(engine):
    from src.ai.decision import AIDecision
    d = AIDecision(action="HOLD", confidence=90.0, reasoning="", model="test", ticker="X")
    assert engine.should_trade(d) is False
