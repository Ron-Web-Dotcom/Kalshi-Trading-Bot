"""Central configuration — all settings loaded from environment variables."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in ("true", "1", "yes")


def _env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class KalshiConfig:
    api_key_id: str = field(default_factory=lambda: _env("KALSHI_API_KEY_ID"))
    private_key_path: str = field(default_factory=lambda: _env("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem"))
    private_key_pem: str = field(default_factory=lambda: _env("KALSHI_PRIVATE_KEY_PEM", ""))
    # Simple API key for demo/basic auth fallback
    api_key: str = field(default_factory=lambda: _env("KALSHI_API_KEY"))
    use_demo: bool = field(default_factory=lambda: _env_bool("KALSHI_USE_DEMO", True))
    base_url: str = field(default_factory=lambda: _env(
        "KALSHI_BASE_URL",
        "https://demo-api.kalshi.co/trade-api/v2" if _env_bool("KALSHI_USE_DEMO", True)
        else "https://api.elections.kalshi.com/trade-api/v2"
    ))
    rate_limit_per_second: int = field(default_factory=lambda: _env_int("KALSHI_RATE_LIMIT", 10))
    timeout: int = field(default_factory=lambda: _env_int("KALSHI_TIMEOUT", 30))


@dataclass
class TradingConfig:
    # Paper vs live
    paper_trading_mode: bool = field(default_factory=lambda: not _env_bool("LIVE_TRADING_ENABLED", False))
    live_trading_enabled: bool = field(default_factory=lambda: _env_bool("LIVE_TRADING_ENABLED", False))

    # Sizing
    base_trade_size_dollars: float = field(default_factory=lambda: _env_float("BASE_TRADE_SIZE", 10.0))
    max_trade_size_dollars: float = field(default_factory=lambda: _env_float("MAX_TRADE_SIZE", 100.0))
    min_trade_size_dollars: float = field(default_factory=lambda: _env_float("MIN_TRADE_SIZE", 1.0))
    max_position_size_pct: float = field(default_factory=lambda: _env_float("MAX_POSITION_PCT", 3.0))

    # Risk
    max_daily_loss_pct: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS_PCT", 10.0))
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 15.0))
    max_sector_exposure_pct: float = field(default_factory=lambda: _env_float("MAX_SECTOR_EXPOSURE_PCT", 30.0))
    cooldown_between_trades_seconds: int = field(default_factory=lambda: _env_int("TRADE_COOLDOWN_SECONDS", 30))
    stop_loss_pct: float = field(default_factory=lambda: _env_float("STOP_LOSS_PCT", 25.0))
    take_profit_pct: float = field(default_factory=lambda: _env_float("TAKE_PROFIT_PCT", 40.0))

    # Portfolio value used for risk % calculations (updated dynamically from API when live)
    portfolio_value: float = field(default_factory=lambda: _env_float("PORTFOLIO_VALUE", 1000.0))

    # Arbitrage
    arbitrage_threshold_pct: float = field(default_factory=lambda: _env_float("ARBITRAGE_THRESHOLD_PCT", 5.0))
    avoid_overtrading_minutes: int = field(default_factory=lambda: _env_int("OVERTRADE_COOLDOWN_MINUTES", 5))

    # Cycle limits (configurable without code changes)
    max_trades_per_cycle: int = field(default_factory=lambda: _env_int("MAX_TRADES_PER_CYCLE", 50))
    max_trades_per_day: int   = field(default_factory=lambda: _env_int("MAX_TRADES_PER_DAY", 50))
    max_markets_to_scan: int  = field(default_factory=lambda: _env_int("MAX_MARKETS_TO_SCAN", 999999))
    min_market_volume: float  = field(default_factory=lambda: _env_float("MIN_MARKET_VOLUME", 0.0))

    # Kelly criterion
    kelly_fraction: float = field(default_factory=lambda: _env_float("KELLY_FRACTION", 0.25))

    # AI thresholds — 70% minimum confidence floor; tiers: 70-79 WATCH, 80-87 BID, 88+ FULL BID
    min_ai_confidence: float = field(default_factory=lambda: _env_float("MIN_AI_CONFIDENCE", 70.0))
    min_confidence_to_trade: float = field(default_factory=lambda: _env_float("MIN_CONFIDENCE_TO_TRADE", 0.70))

    # Minimum profit requirements — need real edge, not statistical noise
    min_profit_roi_pct: float = field(default_factory=lambda: _env_float("MIN_PROFIT_ROI_PCT", 1.0))
    min_profit_abs_usd: float = field(default_factory=lambda: _env_float("MIN_PROFIT_ABS_USD", 0.05))

    # AI position re-evaluation — check open positions against fresh data each cycle
    enable_ai_reeval: bool  = field(default_factory=lambda: _env_bool("ENABLE_AI_REEVAL", True))
    reeval_min_confidence: float = field(default_factory=lambda: _env_float("REEVAL_MIN_CONFIDENCE", 75.0))

    # AI budget — $5 daily soft limit, $15 hard stop (150% of $10 cap)
    daily_ai_budget: float = field(default_factory=lambda: _env_float("DAILY_AI_BUDGET", 5.0))
    daily_ai_hard_cap: float = field(default_factory=lambda: _env_float("DAILY_AI_HARD_CAP", 15.0))
    enable_daily_cost_limiting: bool = field(default_factory=lambda: _env_bool("ENABLE_COST_LIMITING", True))
    sleep_when_limit_reached: bool = field(default_factory=lambda: _env_bool("SLEEP_ON_LIMIT", True))

    # Auto-scaling
    enable_auto_scaling: bool = field(default_factory=lambda: _env_bool("ENABLE_AUTO_SCALING", True))
    scale_up_profit_milestone: float = field(default_factory=lambda: _env_float("SCALE_UP_MILESTONE", 50.0))
    scale_up_factor: float = field(default_factory=lambda: _env_float("SCALE_UP_FACTOR", 1.25))
    scale_down_loss_milestone: float = field(default_factory=lambda: _env_float("SCALE_DOWN_MILESTONE", 25.0))
    scale_down_factor: float = field(default_factory=lambda: _env_float("SCALE_DOWN_FACTOR", 0.8))

    # Production safety features
    kill_switch_enabled: bool = field(default_factory=lambda: _env_bool("KILL_SWITCH_ENABLED", True))
    max_daily_loss_usd: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS_USD", 50.0))
    max_consecutive_losses: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_LOSSES", 5))
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", 50))


@dataclass
class PolymarketConfig:
    api_key:              str   = field(default_factory=lambda: _env("POLY_API_KEY"))
    api_secret:           str   = field(default_factory=lambda: _env("POLY_API_SECRET"))
    wallet_address:       str   = field(default_factory=lambda: _env("POLY_WALLET_ADDRESS", ""))
    live_trading_enabled: bool  = field(default_factory=lambda: _env_bool("POLY_LIVE_TRADING", False))
    enabled:              bool  = field(default_factory=lambda: _env_bool("POLY_ENABLED", True))
    min_order_usdc:       float = field(default_factory=lambda: _env_float("POLY_MIN_ORDER", 1.0))


@dataclass
class AIConfig:
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: _env("AI_MODEL", "gpt-4o-mini"))
    max_tokens: int = field(default_factory=lambda: _env_int("AI_MAX_TOKENS", 1024))
    temperature: float = field(default_factory=lambda: _env_float("AI_TEMPERATURE", 0.3))
    enabled: bool = field(default_factory=lambda: _env_bool("AI_ENABLED", True))


@dataclass
class AlertsConfig:
    discord_webhook_url: str = field(default_factory=lambda: _env("DISCORD_WEBHOOK_URL"))
    discord_enabled: bool = field(default_factory=lambda: bool(_env("DISCORD_WEBHOOK_URL")))
    alert_on_trade: bool = field(default_factory=lambda: _env_bool("ALERT_ON_TRADE", True))
    alert_on_signal: bool = field(default_factory=lambda: _env_bool("ALERT_ON_SIGNAL", True))
    alert_on_error: bool = field(default_factory=lambda: _env_bool("ALERT_ON_ERROR", True))


@dataclass
class LoggingConfig:
    level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    enable_file_logging: bool = field(default_factory=lambda: _env_bool("ENABLE_FILE_LOGGING", True))
    enable_console_logging: bool = field(default_factory=lambda: _env_bool("ENABLE_CONSOLE_LOGGING", True))
    log_dir: str = field(default_factory=lambda: _env("LOG_DIR", "logs"))


@dataclass
class DatabaseConfig:
    url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///trading_system.db"))

    @property
    def path(self) -> str:
        return self.url.replace("sqlite:///", "")


@dataclass
class Settings:
    kalshi:     KalshiConfig     = field(default_factory=KalshiConfig)
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    trading:    TradingConfig    = field(default_factory=TradingConfig)
    ai:         AIConfig         = field(default_factory=AIConfig)
    alerts:     AlertsConfig     = field(default_factory=AlertsConfig)
    logging:    LoggingConfig    = field(default_factory=LoggingConfig)
    database:   DatabaseConfig   = field(default_factory=DatabaseConfig)


def _warn_missing_env_vars() -> None:
    """Log warnings for env vars required at runtime but not set."""
    import logging
    _log = logging.getLogger("trading.settings")
    required = {
        "KALSHI_API_KEY_ID": "Kalshi RSA auth — market data will fail",
        "OPENAI_API_KEY":    "GPT-4o-mini AI decisions — rule-based fallback will be used",
    }
    optional_warned = {
        "DISCORD_WEBHOOK_URL": "Discord alerts disabled — set to get trade notifications",
    }
    for var, note in required.items():
        if not os.environ.get(var):
            _log.warning("Missing env var %s: %s", var, note)
    for var, note in optional_warned.items():
        if not os.environ.get(var):
            _log.info("Optional env var %s not set: %s", var, note)


# Singleton
settings = Settings()
_warn_missing_env_vars()
