"""Tests for RiskManager and AutoScaler."""

import pytest
from src.risk.manager import RiskManager
from src.risk.scaling import AutoScaler


class TestRiskManager:
    def setup_method(self):
        self.risk = RiskManager(db=None)

    def test_allows_valid_trade(self):
        allowed, reason = self.risk.check_trade("TEST-1", 10.0, [], 1000.0)
        assert allowed is True
        assert reason == ""

    def test_blocks_oversized_trade(self):
        allowed, reason = self.risk.check_trade("TEST-1", 999.0, [], 1000.0)
        assert allowed is False
        assert "max" in reason.lower() or "size" in reason.lower()

    def test_cooldown_after_trade(self):
        self.risk.record_trade("COOL-1")
        allowed, reason = self.risk.check_trade("COOL-1", 5.0, [], 1000.0)
        assert allowed is False
        assert "Cooldown" in reason

    def test_different_tickers_not_affected_by_cooldown(self):
        self.risk.record_trade("COOL-1")
        allowed, reason = self.risk.check_trade("COOL-2", 5.0, [], 1000.0)
        assert allowed is True

    def test_daily_loss_circuit_breaker(self):
        # record_result tracks daily loss; record_trade only tracks cooldown
        self.risk.record_result("LOSS-1", pnl=-150.0)  # 15% of $1000 portfolio
        allowed, reason = self.risk.check_trade("ANY-1", 5.0, [], 1000.0)
        assert allowed is False
        assert "Daily loss" in reason

    def test_clamp_size_respects_min(self):
        size = self.risk.clamp_size(0.01)
        assert size >= 1.0  # MIN_TRADE_SIZE default

    def test_clamp_size_respects_max(self):
        size = self.risk.clamp_size(99999.0)
        assert size <= 100.0  # MAX_TRADE_SIZE default

    def test_dollars_to_contracts(self):
        contracts = self.risk.dollars_to_contracts(10.0, 50.0)
        assert contracts == 20  # $10 / ($0.50 per contract) = 20

    def test_dollars_to_contracts_zero_price(self):
        contracts = self.risk.dollars_to_contracts(10.0, 0.0)
        assert contracts == 0


class TestAutoScaler:
    def setup_method(self):
        self.scaler = AutoScaler()

    def test_initial_size_is_base(self):
        assert self.scaler.current_size == 10.0
        assert self.scaler.scale_factor == 1.0

    def test_scale_up_on_profit_milestone(self):
        # First call bootstraps _last_scale_pnl; second call triggers delta check
        self.scaler.update(0.0)   # bootstrap
        self.scaler.update(60.0)  # +$60 delta crosses $50 milestone
        assert self.scaler.scale_factor > 1.0
        assert self.scaler.current_size > 10.0

    def test_scale_down_on_loss_milestone(self):
        # First simulate a gain so scale_up triggers, then drop significantly
        self.scaler.update(60.0)
        factor_after_gain = self.scaler.scale_factor
        self.scaler.update(60.0 - 30.0)  # -$30 from last scale point
        assert self.scaler.scale_factor < factor_after_gain

    def test_size_never_exceeds_max(self):
        self.scaler.update(0.0)  # bootstrap _last_scale_pnl
        for _ in range(20):
            self.scaler.update(self.scaler._last_scale_pnl + 60)
        assert self.scaler.current_size <= 100.0  # MAX_TRADE_SIZE

    def test_size_never_goes_below_min(self):
        self.scaler.update(0.0)  # bootstrap _last_scale_pnl
        for _ in range(20):
            self.scaler.update(self.scaler._last_scale_pnl - 30)
        assert self.scaler.current_size >= 1.0  # MIN_TRADE_SIZE
