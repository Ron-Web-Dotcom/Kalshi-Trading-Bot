"""Tests for ArbitrageDetector."""

import pytest
from src.strategy.arbitrage import ArbitrageDetector


@pytest.fixture
def detector():
    return ArbitrageDetector()


def test_no_signal_below_threshold(detector):
    comparisons = [{"kalshi_ticker": "A", "kalshi_price": 50, "poly_price": 52, "diff_pct": 4.0}]
    signals = detector.detect(comparisons)
    assert signals == []


def test_signal_above_threshold(detector):
    comparisons = [{"kalshi_ticker": "A", "kalshi_price": 50, "poly_price": 60, "diff_pct": 20.0}]
    signals = detector.detect(comparisons)
    assert len(signals) == 1
    assert signals[0]["ticker"] == "A"
    assert signals[0]["action"] == "BUY"


def test_buys_yes_when_kalshi_cheaper(detector):
    comparisons = [{"kalshi_ticker": "B", "kalshi_price": 40, "poly_price": 60, "diff_pct": 33.0}]
    signals = detector.detect(comparisons)
    assert signals[0]["side"] == "yes"


def test_buys_no_when_kalshi_expensive(detector):
    comparisons = [{"kalshi_ticker": "C", "kalshi_price": 70, "poly_price": 50, "diff_pct": 40.0}]
    signals = detector.detect(comparisons)
    assert signals[0]["side"] == "no"


def test_cooldown_prevents_second_signal(detector):
    comparisons = [{"kalshi_ticker": "D", "kalshi_price": 40, "poly_price": 60, "diff_pct": 33.0}]
    detector.detect(comparisons)  # first — records cooldown
    signals = detector.detect(comparisons)  # second — should be blocked
    assert signals == []


def test_different_tickers_not_cooled_down(detector):
    c1 = [{"kalshi_ticker": "E1", "kalshi_price": 40, "poly_price": 60, "diff_pct": 33.0}]
    c2 = [{"kalshi_ticker": "E2", "kalshi_price": 40, "poly_price": 60, "diff_pct": 33.0}]
    detector.detect(c1)
    signals = detector.detect(c2)
    assert len(signals) == 1


def test_internal_arb_detected(detector):
    markets = [{"ticker": "INT-1", "yes_ask": 45, "no_ask": 45, "yes_bid": 43, "no_bid": 43}]
    signals = detector.detect_internal(markets)
    assert len(signals) == 1
    assert signals[0]["edge_cents"] == 10  # 100 - 45 - 45


def test_internal_arb_not_detected_when_sum_100(detector):
    markets = [{"ticker": "INT-2", "yes_ask": 50, "no_ask": 50, "yes_bid": 48, "no_bid": 48}]
    signals = detector.detect_internal(markets)
    assert signals == []


def test_internal_arb_not_detected_below_threshold(detector):
    # 100 - 51 - 48 = 1¢ edge, below 5% default threshold
    markets = [{"ticker": "INT-3", "yes_ask": 51, "no_ask": 48, "yes_bid": 49, "no_bid": 46}]
    signals = detector.detect_internal(markets)
    assert signals == []
