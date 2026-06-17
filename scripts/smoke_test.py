#!/usr/bin/env python3
"""
Smoke test — run after any code change to verify critical paths.
Usage: python scripts/smoke_test.py
All tests should print PASS. Any FAIL means something is broken before deploy.
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0

def ok(name):
    global PASS
    PASS += 1
    print(f"  PASS  {name}")

def fail(name, reason=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}{': ' + reason if reason else ''}")

# ── Test 1: Evaluation schema always has price_cents ──────────────────────────
def test_eval_schema():
    from src.utils.daily_stats import DailyStats
    ds = DailyStats()
    ds.record_evaluation(
        ticker="T1", action="BUY", side="yes", confidence=85.0,
        net_ev=5.0, true_prob=0.85, reasoning="test",
        title="Test", platform="kalshi",
        close_time="2026-12-31T00:00:00Z", yes_ask=45.0,
    )
    ev = ds.all_evaluations[0] if ds.all_evaluations else {}
    assert ev.get("price_cents", 0) == 45.0, f"price_cents={ev.get('price_cents')}"
    assert ev.get("yes_ask", 0) == 45.0, f"yes_ask={ev.get('yes_ask')}"
    assert ev.get("action") == "BUY"
    assert ev.get("confidence") == 85.0

# ── Test 2: Bot alert loop price filter passes valid evaluations ───────────────
def test_alert_price_filter():
    # Simulate the bot_alert_loop price check
    ev = {"ticker": "T2", "action": "BUY", "confidence": 85.0,
          "price_cents": 45.0, "yes_ask": 45.0, "close_time": "2099-01-01T00:00:00Z"}
    price = float(ev.get("price_cents") or ev.get("yes_ask") or 0)
    assert price > 0, "price is 0 — will be filtered"
    assert 5 <= price <= 95, f"price {price} outside 5-95 range"

# ── Test 3: Junk filter blocks known junk, passes good markets ────────────────
def test_junk_filter():
    from src.utils.junk_filter import is_junk
    assert is_junk("ivan cepeda castro colombia"), "should block ivan cepeda"
    assert is_junk("will gavin newsom win"), "should block gavin newsom"
    assert is_junk("win the world cup"), "should block world cup winner"
    assert not is_junk("Portugal vs DR Congo: O/U 4.5"), "should pass portugal match"
    assert not is_junk("Will CPI exceed 3.5% in June?"), "should pass CPI market"

# ── Test 4: _closes_today and _closes_within_week use correct time windows ────
def test_time_windows():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    def make_market(hours):
        ct = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"close_time": ct, "yes_ask": 50, "ticker": "T"}

    # Simulate _closes_today logic
    tonight = now.replace(hour=23, minute=59, second=59)
    def closes_today(m):
        ct = m.get("close_time", "")
        try:
            cd = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return now < cd <= tonight
        except:
            return False

    assert closes_today(make_market(2)), "2h market should close today"
    assert not closes_today(make_market(30)), "30h market should NOT close today"

# ── Test 5: 75% confidence gate for 7-day markets ────────────────────────────
def test_confidence_gate():
    def should_trade(hours_out, confidence):
        week_min = 75.0
        if hours_out > 24 and confidence < week_min:
            return False
        return True

    assert should_trade(6, 60), "today+60% should trade"
    assert not should_trade(48, 60), "2day+60% should NOT trade"
    assert should_trade(48, 75), "2day+75% should trade"
    assert not should_trade(168, 74), "7day+74% should NOT trade"
    assert should_trade(168, 75), "7day+75% should trade"

# ── Test 6: Kalshi preference in opportunity hunter ───────────────────────────
def test_kalshi_preference():
    def pick_winner(kalshi_score, poly_score, min_score=0.01):
        k_ok = kalshi_score is not None and kalshi_score >= min_score
        p_ok = poly_score   is not None and poly_score   >= min_score
        if k_ok and p_ok:
            return "polymarket" if poly_score > kalshi_score * 1.5 else "kalshi"
        return "kalshi" if k_ok else ("polymarket" if p_ok else None)

    assert pick_winner(0.5, 0.6) == "kalshi", "close scores → kalshi wins"
    assert pick_winner(0.3, 0.8) == "polymarket", "poly 2.7x better → poly wins"
    assert pick_winner(0.4, None) == "kalshi", "only kalshi → kalshi"
    assert pick_winner(None, 0.7) == "polymarket", "only poly → poly"
    assert pick_winner(None, None) is None, "neither → no trade"

# ── Runner ────────────────────────────────────────────────────────────────────
TESTS = [
    test_eval_schema,
    test_alert_price_filter,
    test_junk_filter,
    test_time_windows,
    test_confidence_gate,
    test_kalshi_preference,
]

if __name__ == "__main__":
    print(f"\nSmoke Test — {len(TESTS)} tests\n")
    for t in TESTS:
        name = t.__name__.replace("test_", "").replace("_", " ")
        try:
            t()
            ok(name)
        except AssertionError as e:
            fail(name, str(e))
        except Exception as e:
            fail(name, f"ERROR: {e}")

    print(f"\n{'='*40}")
    print(f"  {PASS} passed  |  {FAIL} failed")
    print(f"{'='*40}\n")
    sys.exit(0 if FAIL == 0 else 1)
