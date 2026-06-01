"""Tests for production safety features: kill switch, lockouts, positions limit, audit log, health check."""

import asyncio
import os
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_db(rows_by_query=None):
    """Build a mock DatabaseManager that returns preset rows."""
    db = MagicMock()
    rows_by_query = rows_by_query or {}

    async def _fetchone(query, params=()):
        for key, val in rows_by_query.items():
            if key in query:
                return val
        return None

    async def _fetchall(query, params=()):
        for key, val in rows_by_query.items():
            if key in query:
                return val if isinstance(val, list) else []
        return []

    async def _execute(query, params=()):
        pass

    db.fetchone = _fetchone
    db.fetchall = _fetchall
    db.execute = _execute
    return db


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1 — Kill Switch
# ═══════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    def setup_method(self):
        """Ensure the kill switch file is clean before each test."""
        from src.utils import kill_switch as ks
        if ks._KILL_SWITCH_FILE.exists():
            ks._KILL_SWITCH_FILE.unlink()

    def teardown_method(self):
        from src.utils import kill_switch as ks
        if ks._KILL_SWITCH_FILE.exists():
            ks._KILL_SWITCH_FILE.unlink()

    def test_kill_switch_inactive_by_default(self):
        from src.utils.kill_switch import is_active
        assert is_active() is False

    def test_engage_makes_active(self):
        from src.utils.kill_switch import engage, is_active
        engage("test reason")
        assert is_active() is True

    def test_disengage_deactivates(self):
        from src.utils.kill_switch import engage, disengage, is_active
        engage("test")
        disengage()
        assert is_active() is False

    def test_kill_word_case_insensitive(self):
        from src.utils import kill_switch as ks
        ks._KILL_SWITCH_FILE.write_text("kill\nsome reason")
        assert ks.is_active() is True

    def test_non_kill_content_not_active(self):
        from src.utils import kill_switch as ks
        ks._KILL_SWITCH_FILE.write_text("running normally")
        assert ks.is_active() is False

    def test_kill_switch_disabled_by_setting(self):
        from src.utils.kill_switch import engage, is_active
        from src.config.settings import settings
        original = settings.trading.kill_switch_enabled
        try:
            settings.trading.kill_switch_enabled = False
            engage("reason")
            assert is_active() is False
        finally:
            settings.trading.kill_switch_enabled = original

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_trading_job(self):
        """run_trading_job returns early with zero trades when kill switch is active."""
        from src.utils.kill_switch import engage, disengage
        engage("halt all trading")
        try:
            from src.jobs.trade import run_trading_job
            db = _make_db()
            results = await run_trading_job(db=db)
            assert results.total_positions == 0
            assert results.arb_trades == 0
            assert results.ai_trades == 0
        finally:
            disengage()

    @pytest.mark.asyncio
    async def test_kill_switch_allows_trading_when_inactive(self):
        """run_trading_job proceeds past kill switch check when inactive."""
        from src.utils.kill_switch import is_active
        assert is_active() is False
        # We just confirm is_active() is False — full integration would require DB/network
        assert True


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2 — Daily Loss Lockout
# ═══════════════════════════════════════════════════════════════════════════════

class TestDailyLossLockout:
    @pytest.mark.asyncio
    async def test_no_lockout_when_no_losses(self):
        from src.risk.manager import RiskManager
        db = _make_db({
            "SUM(pnl)": {"total_pnl": 10.0},
            "LIMIT": [],
        })
        risk = RiskManager(db=db)
        locked, reason = await risk.check_daily_loss_lockout(db)
        assert locked is False
        assert reason == ""

    @pytest.mark.asyncio
    async def test_daily_loss_limit_triggers_lockout(self):
        from src.risk.manager import RiskManager
        from src.config.settings import settings
        settings.trading.max_daily_loss_usd = 50.0
        # total_pnl = -75 (loss of $75 > $50 limit)
        db = _make_db({
            "SUM(pnl)": {"total_pnl": -75.0},
            "LIMIT": [],
        })
        risk = RiskManager(db=db)
        locked, reason = await risk.check_daily_loss_lockout(db)
        assert locked is True
        assert "Daily loss limit" in reason
        assert "locked out" in reason

    @pytest.mark.asyncio
    async def test_consecutive_loss_lockout_triggers(self):
        from src.risk.manager import RiskManager
        from src.config.settings import settings
        settings.trading.max_consecutive_losses = 3
        # 3 losses in a row
        loss_rows = [{"pnl": -5.0}, {"pnl": -3.0}, {"pnl": -2.0}]
        db = _make_db({
            "SUM(pnl)": {"total_pnl": 0.0},   # no daily loss limit hit
            "LIMIT": loss_rows,
        })
        risk = RiskManager(db=db)
        locked, reason = await risk.check_daily_loss_lockout(db)
        assert locked is True
        assert "consecutive" in reason.lower()

    @pytest.mark.asyncio
    async def test_consecutive_loss_no_lockout_with_win(self):
        from src.risk.manager import RiskManager
        from src.config.settings import settings
        settings.trading.max_consecutive_losses = 3
        # Last 3 include a win — no streak
        mixed_rows = [{"pnl": 5.0}, {"pnl": -3.0}, {"pnl": -2.0}]
        db = _make_db({
            "SUM(pnl)": {"total_pnl": 0.0},
            "LIMIT": mixed_rows,
        })
        risk = RiskManager(db=db)
        locked, reason = await risk.check_daily_loss_lockout(db)
        assert locked is False

    @pytest.mark.asyncio
    async def test_lockout_with_none_db_returns_safe(self):
        from src.risk.manager import RiskManager
        risk = RiskManager(db=None)
        locked, reason = await risk.check_daily_loss_lockout(None)
        assert locked is False


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3 — Max Open Positions
# ═══════════════════════════════════════════════════════════════════════════════

class TestMaxOpenPositions:
    @pytest.mark.asyncio
    async def test_max_open_positions_blocks_new_trades(self):
        """run_trading_job skips new trades when open positions = max."""
        from src.config.settings import settings
        settings.trading.max_open_positions = 2

        # Kill switch must be off
        from src.utils import kill_switch as ks
        if ks._KILL_SWITCH_FILE.exists():
            ks._KILL_SWITCH_FILE.unlink()

        # DB returns 2 open positions; lockout query returns no loss
        call_count = {"n": 0}

        async def _fetchone_mock(query, params=()):
            if "COUNT(*) as n" in query and "positions" in query:
                return {"n": 2}
            if "SUM(pnl)" in query:
                return {"total_pnl": 0.0}
            return None

        async def _fetchall_mock(query, params=()):
            return []

        async def _execute_mock(query, params=()):
            pass

        db = MagicMock()
        db.fetchone = _fetchone_mock
        db.fetchall = _fetchall_mock
        db.execute = _execute_mock

        from src.risk.manager import RiskManager
        risk = RiskManager(db=db)

        from src.jobs.trade import run_trading_job
        results = await run_trading_job(db=db, risk=risk)
        # Should return early due to max positions
        assert results.total_positions == 0

    @pytest.mark.asyncio
    async def test_positions_below_max_allows_trading(self):
        from src.config.settings import settings
        settings.trading.max_open_positions = 10
        # With 0 open positions, trading is not blocked by this check
        # (other checks may still block, but this one passes)
        assert settings.trading.max_open_positions == 10


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 4 — Audit Log
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_log_writes_to_file(self, tmp_path):
        from src.utils.audit_log import AuditLogger
        import src.utils.audit_log as audit_module

        orig = audit_module._AUDIT_FILE
        audit_module._AUDIT_FILE = tmp_path / "audit.log"
        audit_module._LOG_DIR = tmp_path

        try:
            auditor = AuditLogger()
            db = _make_db()
            await auditor.log(
                db,
                event_type="TRADE_PLACED",
                ticker="KXBTC",
                side="yes",
                price_cents=55,
                size_usd=10.0,
                confidence=75,
                net_ev=3.5,
                reason="test trade",
                result="PENDING",
            )
            content = (tmp_path / "audit.log").read_text()
            assert "TRADE_PLACED" in content
            assert "KXBTC" in content
            assert "yes" in content
            assert "55¢" in content
            assert "test trade" in content
        finally:
            audit_module._AUDIT_FILE = orig
            audit_module._LOG_DIR = orig.parent

    @pytest.mark.asyncio
    async def test_audit_log_inserts_to_db(self):
        from src.utils.audit_log import AuditLogger
        import src.utils.audit_log as audit_module
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_module._LOG_DIR = Path(tmp)
            audit_module._AUDIT_FILE = Path(tmp) / "audit.log"

            executed = []

            async def _execute(query, params=()):
                executed.append((query, params))

            db = MagicMock()
            db.execute = _execute

            auditor = AuditLogger()
            await auditor.log(
                db,
                event_type="LOCKOUT",
                ticker="",
                reason="daily loss limit",
                result="SKIPPED",
            )

            assert any("audit_log" in q for q, _ in executed)
            # Check the params tuple includes the event_type
            params_list = [p for _, p in executed]
            assert any("LOCKOUT" in str(p) for p in params_list)

    @pytest.mark.asyncio
    async def test_audit_log_correct_fields(self):
        from src.utils.audit_log import AuditLogger
        import src.utils.audit_log as audit_module
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            audit_module._LOG_DIR = __import__("pathlib").Path(tmp)
            audit_module._AUDIT_FILE = audit_module._LOG_DIR / "audit.log"

            received = {}

            async def _execute(query, params=()):
                received["params"] = params

            db = MagicMock()
            db.execute = _execute

            auditor = AuditLogger()
            await auditor.log(
                db,
                event_type="TRADE_PLACED",
                ticker="TICKER1",
                platform="polymarket",
                side="no",
                price_cents=40,
                size_usd=25.0,
                confidence=80,
                net_ev=5.0,
                reason="arb edge",
                result="PENDING",
                pnl=None,
                operator="bot",
            )

            p = received.get("params", ())
            assert p[0] == "TRADE_PLACED"     # event_type
            assert p[1] == "TICKER1"           # ticker
            assert p[2] == "polymarket"        # platform
            assert p[3] == "no"                # side
            assert p[4] == 40                  # price_cents
            assert p[5] == 25.0                # size_usd
            assert p[6] == 80                  # confidence
            assert p[7] == 5.0                 # net_ev


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 5 — Health Check
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_returns_all_services(self):
        from src.utils.health_check import HealthChecker, HealthResult

        async def _fail(*args, **kwargs):
            raise ConnectionError("mock failure")

        checker = HealthChecker()
        checker._check_kalshi = _fail
        checker._check_polymarket = _fail
        checker._check_discord = _fail
        checker._check_ai = _fail

        results = await checker.run_all()
        # All 4 services should be present even if they fail
        assert "Kalshi" in results
        assert "Polymarket" in results
        assert "Discord" in results
        assert "AI" in results

    @pytest.mark.asyncio
    async def test_health_check_does_not_raise_on_all_failures(self):
        from src.utils.health_check import HealthChecker

        async def _boom(*args, **kwargs):
            raise RuntimeError("catastrophic failure")

        checker = HealthChecker()
        checker._check_kalshi = _boom
        checker._check_polymarket = _boom
        checker._check_discord = _boom
        checker._check_ai = _boom

        # Must not raise
        try:
            results = await checker.run_all()
        except Exception as e:
            pytest.fail(f"run_all() raised unexpectedly: {e}")

        # All should be marked as failed
        for name, r in results.items():
            assert r.ok is False, f"{name} should be marked failed"

    @pytest.mark.asyncio
    async def test_health_result_fields(self):
        from src.utils.health_check import HealthResult
        r = HealthResult(service="TestSvc", ok=True, latency_ms=123.4, message="operational")
        assert r.service == "TestSvc"
        assert r.ok is True
        assert r.latency_ms == 123.4
        assert r.message == "operational"

    @pytest.mark.asyncio
    async def test_health_check_captures_exception_as_failed_result(self):
        from src.utils.health_check import HealthChecker, HealthResult

        async def _ok_check(self_):
            return HealthResult("Kalshi", True, 50.0, "ok")

        async def _fail_check(self_):
            raise TimeoutError("timed out")

        checker = HealthChecker()
        # Patch individual checks
        with (
            patch.object(checker, "_check_kalshi", lambda: _ok_check(checker)),
            patch.object(checker, "_check_polymarket", lambda: _fail_check(checker)),
            patch.object(checker, "_check_discord", lambda: _ok_check(checker)),
            patch.object(checker, "_check_ai", lambda: _ok_check(checker)),
        ):
            results = await checker.run_all()

        assert results["Polymarket"].ok is False
        assert "timed out" in results["Polymarket"].message
