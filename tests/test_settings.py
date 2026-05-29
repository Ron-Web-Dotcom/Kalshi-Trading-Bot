"""Tests for settings / configuration."""

import os
import pytest
from src.config.settings import Settings, TradingConfig


def test_defaults_are_paper_mode():
    s = Settings()
    assert s.trading.paper_trading_mode is True
    assert s.trading.live_trading_enabled is False


def test_live_mode_via_env(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    s = Settings()
    assert s.trading.live_trading_enabled is True
    assert s.trading.paper_trading_mode is False


def test_trade_size_defaults():
    s = Settings()
    assert s.trading.base_trade_size_dollars == 10.0
    assert s.trading.max_trade_size_dollars == 100.0
    assert s.trading.min_trade_size_dollars == 1.0


def test_risk_defaults():
    s = Settings()
    assert s.trading.max_daily_loss_pct == 10.0
    assert s.trading.max_drawdown_pct == 15.0
    assert s.trading.arbitrage_threshold_pct == 5.0
    assert s.trading.kelly_fraction == 0.25
    assert s.trading.min_confidence_to_trade == 0.45


def test_ai_defaults():
    s = Settings()
    assert s.ai.model == "claude-sonnet-4-6"
    assert s.ai.max_tokens == 1024
    assert s.ai.temperature == 0.3
    assert s.trading.min_ai_confidence == 70.0


def test_discord_disabled_without_webhook():
    s = Settings()
    # Without DISCORD_WEBHOOK_URL set, discord should be disabled
    if not os.environ.get("DISCORD_WEBHOOK_URL"):
        assert s.alerts.discord_enabled is False


def test_settings_mutation():
    s = Settings()
    s.trading.live_trading_enabled = True
    s.trading.paper_trading_mode = False
    assert s.trading.live_trading_enabled is True
    assert s.trading.paper_trading_mode is False
