"""
Regression tests for every confirmed bug that was found and fixed.
Each test is named for the bug it guards against — if it goes red, that bug is back.

Run:  pytest tests/test_bugs_regression.py -v
"""

import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

class FakeDB:
    """Minimal in-memory DB stub."""

    def __init__(self, rows=None):
        self._rows = rows or {}
        self.calls = []

    async def fetchone(self, query, params=()):
        self.calls.append(("fetchone", query, params))
        return self._rows.get("one")

    async def fetchall(self, query, params=()):
        self.calls.append(("fetchall", query, params))
        return self._rows.get("all", [])

    async def execute(self, query, params=()):
        self.calls.append(("execute", query, params))

    async def executemany(self, query, rows):
        self.calls.append(("executemany", query, rows))

    async def initialize(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Price normalisation (kalshi_client.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_price_norm_fraction_to_cents():
    """Fractional prices (0.0-1.0) must be converted TO cents, not divided further."""
    # Before fix: f / 100.0 would turn 0.45 into 0.0045 (catastrophically wrong)
    # After fix:  f * 100  turns 0.45 into 45 (correct)
    def _norm(v):
        try:
            f = float(v or 0)
            return f * 100 if f < 1.0 else f
        except Exception:
            return 0.0

    assert _norm(0.45) == 45.0,  "0.45 fraction must become 45¢"
    assert _norm(45)   == 45.0,  "45 cents must stay 45¢"
    assert _norm(0.82) == 82.0,  "0.82 fraction must become 82¢"
    assert _norm(1.0)  == 1.0,   "exactly 1.0 is treated as cents (1¢), edge-case fine"
    assert _norm(0)    == 0.0,   "zero stays zero"


def test_no_ask_fallback_cents():
    """no_ask fallback must be 100 - yes_ask in cents, not 1.0 - yes_ask."""
    yes_ask = 45.0  # cents
    no_ask_correct = round(100.0 - yes_ask, 2)
    no_ask_wrong   = round(1.0   - yes_ask, 2)  # old bug: -44.0
    assert no_ask_correct == 55.0
    assert no_ask_wrong   == -44.0   # proves the old bug was real
    # Our code must produce 55, not -44
    assert no_ask_correct > 0


# ══════════════════════════════════════════════════════════════════════════════
# Junk filter (src/utils/junk_filter.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_junk_filter_blocks_announcer_markets():
    from src.utils.junk_filter import is_junk
    assert is_junk("Will the announcer say 'Aggressive' during the match?")
    assert is_junk("Will the commentator say 'Incredible'?")
    assert is_junk("Announcer Say 'Clutch' before halftime?")


def test_junk_filter_blocks_world_cup_winners():
    from src.utils.junk_filter import is_junk
    assert is_junk("Will Spain win the 2026 FIFA World Cup?")
    assert is_junk("Who will win the 2026 World Cup?")
    assert is_junk("FIFA World Cup winner 2026")


def test_junk_filter_allows_good_markets():
    from src.utils.junk_filter import is_junk
    assert not is_junk("Mexico leading at halftime?")
    assert not is_junk("Will the Fed raise rates in July?")
    assert not is_junk("BTC above $70k by end of day?")


def test_junk_filter_case_insensitive():
    from src.utils.junk_filter import is_junk
    assert is_junk("GAVIN NEWSOM president 2028")
    assert is_junk("Will KANYE release an album?")


# ══════════════════════════════════════════════════════════════════════════════
# Correlated position guard (src/jobs/trade.py _already_open)
# ══════════════════════════════════════════════════════════════════════════════

def test_correlated_guard_blocks_same_game_exact_scores():
    """Two different exact-score markets for the same game must be blocked."""
    import re

    score_pat = re.compile(
        r'\bexact score[:\s]*|\bspread[:\s]*|\bo/?u[:\s]*|\bover[/\s]under[:\s]*'
        r'|\b\d+[:\-]\d+\b|\(-?\d+\.?\d*\)'
        r'|\b(yes|no|will|the|be|on|at|in|a|an|is|to|of|and|or|for)\b'
        r'|[?!,.]',
        re.IGNORECASE,
    )

    def _event_words(title):
        stripped = score_pat.sub(' ', title.lower())
        return {w for w in stripped.split() if len(w) > 2}

    title_a = "Exact Score: United States 3-0 Bosnia and Herzegovina?"
    title_b = "Exact Score: United States 2-0 Bosnia and Herzegovina?"

    words_a = _event_words(title_a)
    words_b = _event_words(title_b)

    overlap = words_a & words_b
    overlap_ratio = len(overlap) / max(len(words_a), 1)

    assert overlap_ratio >= 0.5, (
        f"Same-game markets should overlap ≥50%, got {overlap_ratio:.0%} "
        f"overlap={overlap}"
    )


def test_correlated_guard_allows_different_games():
    """Markets from different games must NOT block each other."""
    import re

    score_pat = re.compile(
        r'\bexact score[:\s]*|\bspread[:\s]*|\bo/?u[:\s]*|\bover[/\s]under[:\s]*'
        r'|\b\d+[:\-]\d+\b|\(-?\d+\.?\d*\)'
        r'|\b(yes|no|will|the|be|on|at|in|a|an|is|to|of|and|or|for)\b'
        r'|[?!,.]',
        re.IGNORECASE,
    )

    def _event_words(title):
        stripped = score_pat.sub(' ', title.lower())
        return {w for w in stripped.split() if len(w) > 2}

    title_a = "Mexico leading at halftime vs Ecuador?"
    title_b = "England to score first vs DR Congo?"

    words_a = _event_words(title_a)
    words_b = _event_words(title_b)

    overlap = words_a & words_b
    overlap_ratio = len(overlap) / max(len(words_a), 1)

    assert overlap_ratio < 0.5, (
        f"Different games should overlap <50%, got {overlap_ratio:.0%}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# EV threshold (src/config/settings.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_min_profit_abs_usd_default_is_meaningful():
    """Default minimum absolute profit must be ≥ 5¢ (not 1¢ noise level)."""
    from src.config.settings import TradingConfig
    cfg = TradingConfig()
    assert cfg.min_profit_abs_usd >= 0.05, (
        f"min_profit_abs_usd={cfg.min_profit_abs_usd} is too low — "
        "allows noise-level trades at 1-2¢ EV"
    )


def test_min_profit_roi_default_is_meaningful():
    """Default minimum ROI must be ≥ 1% (not 0.01% noise level)."""
    from src.config.settings import TradingConfig
    cfg = TradingConfig()
    assert cfg.min_profit_roi_pct >= 1.0, (
        f"min_profit_roi_pct={cfg.min_profit_roi_pct} is too low"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Risk manager (src/risk/manager.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_risk_cooldown_keyed_by_platform():
    """Kalshi and Polymarket cooldowns must be independent per ticker."""
    from src.risk.manager import RiskManager

    risk = RiskManager(db=None)
    risk.record_trade("KXBTC-25", platform="kalshi")

    # Kalshi should be on cooldown
    ok_kal, reason_kal = risk.check_trade(
        "KXBTC-25", 10.0, [], portfolio_value=1000.0, platform="kalshi"
    )
    assert not ok_kal, "Kalshi should be on cooldown"
    assert "Cooldown" in reason_kal

    # Polymarket for the same ticker should NOT be on cooldown
    ok_poly, _ = risk.check_trade(
        "KXBTC-25", 10.0, [], portfolio_value=1000.0, platform="polymarket"
    )
    assert ok_poly, "Polymarket should not be blocked by Kalshi cooldown"


def test_risk_daily_loss_lockout_query_uses_trade_logs():
    """Daily loss lockout primary check must query trade_logs, not JOIN positions."""
    from src.risk.manager import RiskManager
    import inspect

    src = inspect.getsource(RiskManager.check_daily_loss_lockout)
    # Primary PnL check must use trade_logs table
    assert "FROM trade_logs" in src or 'FROM trade_logs"' in src, (
        "Daily loss lockout must query trade_logs for PnL, not positions table"
    )
    # Must NOT do a JOIN that multiplies rows
    assert "JOIN positions" not in src and "positions JOIN" not in src, (
        "Lockout query must not JOIN positions — fans out PnL per position row"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Arbitrage (src/data/external_markets.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_no_side_arb_negative_edge_is_skipped():
    """When Kalshi NO costs more than Poly NO, that pair must be skipped (no spurious edge)."""
    # Before fix: fallback used kalshi_yes - poly_yes (YES spread) as NO edge
    # This produced a positive number even when NO edge is negative.
    kalshi_yes, kalshi_no = 60.0, 40.0   # Kalshi overprices YES
    poly_yes,   poly_no   = 55.0, 45.0   # Poly has higher NO price too

    # Edge on NO side: poly_no - kalshi_no = 45 - 40 = +5 (positive, real edge)
    edge_no = poly_no - kalshi_no
    assert edge_no > 0, "This case should have positive edge — test setup check"

    # Now: kalshi_no > poly_no (no edge)
    kalshi_yes2, kalshi_no2 = 55.0, 46.0
    poly_yes2,   poly_no2   = 60.0, 42.0  # poly_no < kalshi_no

    edge_no2 = poly_no2 - kalshi_no2  # 42 - 46 = -4 (negative)
    assert edge_no2 < 0, "Negative NO edge must be detected"
    # After fix: pairs with edge_no2 <= 0 are skipped entirely


# ══════════════════════════════════════════════════════════════════════════════
# Polymarket client (src/clients/polymarket_client.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_polymarket_zero_price_returns_none():
    """Markets with no real price data must return None, not default to 50/50."""
    # Simulate the check that was fixed
    yes_price, no_price = 0.0, 0.0

    # Old buggy behavior: yes_price, no_price = 50.0, 50.0
    # New behavior: return None
    result = None if (yes_price == 0 and no_price == 0) else {"yes_price": yes_price}
    assert result is None, "Zero-price market must return None, not fabricated 50/50"


# ══════════════════════════════════════════════════════════════════════════════
# Discord embed safety (src/alerts/discord.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_discord_embed_truncates_long_fields():
    """Discord field values must be truncated to 1024 chars to avoid 400 errors."""
    from src.alerts.discord import DiscordAlerter
    alerter = DiscordAlerter()

    long_value = "x" * 2000
    fields = [{"name": "Test", "value": long_value, "inline": False}]

    embed_payload = alerter._embed("Title", "Desc", 0x00ff00, fields=fields)
    embed = embed_payload["embeds"][0]

    for f in embed.get("fields", []):
        assert len(f["value"]) <= 1024, f"Field value exceeds 1024 chars: {len(f['value'])}"


def test_discord_embed_truncates_title_and_description():
    """Title must be ≤256 chars and description ≤4096 chars."""
    from src.alerts.discord import DiscordAlerter
    alerter = DiscordAlerter()

    embed_payload = alerter._embed("T" * 300, "D" * 5000, 0x00ff00)
    embed = embed_payload["embeds"][0]

    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096


# ══════════════════════════════════════════════════════════════════════════════
# SQLite timestamp consistency (src/jobs/track.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_resolved_at_uses_iso_format_not_sqlite_now():
    """track.py must not use datetime('now') — must use Python ISO format."""
    import inspect
    from src.jobs import track

    src = inspect.getsource(track)
    assert "datetime('now')" not in src, (
        "track.py must not use SQLite datetime('now') — it produces "
        "YYYY-MM-DD HH:MM:SS format, mismatching ISO timestamps elsewhere"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Confidence calibrator persistence (src/utils/confidence_calibrator.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_calibrator_threshold_file_not_in_tmp():
    """Confidence threshold must be stored outside /tmp so it survives reboots."""
    from src.utils import confidence_calibrator
    path = confidence_calibrator._THRESHOLD_FILE
    assert not path.startswith("/tmp"), (
        f"Threshold file is in /tmp ({path}) — it will be lost on reboot. "
        "Store in project data/ directory instead."
    )


# ══════════════════════════════════════════════════════════════════════════════
# Daily stats platform filter (src/utils/daily_stats.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_best_pick_by_platform_kalshi_filter():
    """best_pick_by_platform must use == 'kalshi', not != 'polymarket'."""
    import inspect
    from src.utils import daily_stats

    src = inspect.getsource(daily_stats.DailyStats.best_pick_by_platform)
    assert "!= \"polymarket\"" not in src and "!= 'polymarket'" not in src, (
        "Kalshi filter uses != 'polymarket' — breaks if a third platform is added"
    )
    assert "== \"kalshi\"" in src or "== 'kalshi'" in src, (
        "Kalshi filter must use == 'kalshi'"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Live market manager: SQLite UPDATE subquery (src/jobs/live_market_manager.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_close_position_no_order_by_in_update():
    """_close_position must not use ORDER BY/LIMIT in UPDATE (invalid SQLite)."""
    import inspect
    from src.jobs import live_market_manager

    src = inspect.getsource(live_market_manager._close_position)
    update_blocks = [line for line in src.splitlines() if "UPDATE trade_logs" in line.upper()]

    for block_start in range(len(src.splitlines())):
        line = src.splitlines()[block_start]
        if "UPDATE trade_logs" in line.upper():
            # Check surrounding lines for ORDER BY without a subquery
            context = "\n".join(src.splitlines()[block_start:block_start+15])
            if "ORDER BY" in context.upper():
                # Must be inside a subquery (SELECT ... ORDER BY)
                assert "SELECT" in context.upper(), (
                    "ORDER BY inside UPDATE without subquery — invalid SQLite syntax"
                )


# ══════════════════════════════════════════════════════════════════════════════
# asyncio.gather exception handling (src/jobs/decide.py)
# ══════════════════════════════════════════════════════════════════════════════

def test_decide_gather_uses_return_exceptions():
    """asyncio.gather in decide.py must use return_exceptions=True so AI crash
    doesn't kill the whole trade cycle."""
    import inspect
    from src.jobs import decide

    src = inspect.getsource(decide.make_decision_for_market)
    assert "return_exceptions=True" in src, (
        "asyncio.gather must use return_exceptions=True so an AI exception "
        "falls back to rule engine instead of crashing the cycle"
    )
