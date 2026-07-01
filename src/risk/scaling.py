"""Phase 8 — auto-scaling: grow size after profits, shrink after losses."""

import logging
from typing import Optional

logger = logging.getLogger("trading.scaling")


class AutoScaler:
    def __init__(self):
        from src.config.settings import settings
        cfg = settings.trading
        self.enabled = cfg.enable_auto_scaling
        self.base_size = cfg.base_trade_size_dollars
        self.scale_up_milestone = cfg.scale_up_profit_milestone
        self.scale_up_factor = cfg.scale_up_factor
        self.scale_down_milestone = cfg.scale_down_loss_milestone
        self.scale_down_factor = cfg.scale_down_factor
        self.max_size = cfg.max_trade_size_dollars
        self.min_size = cfg.min_trade_size_dollars

        self._scale_factor = 1.0
        self._cumulative_pnl = 0.0
        self._last_scale_pnl: Optional[float] = None

    def update(self, new_pnl: float) -> float:
        """
        Update cumulative PnL and return new trade size.
        new_pnl is the ALL-TIME total PnL (not a delta) — pass sum of all closed pnl.
        """
        if not self.enabled:
            return self.base_size

        self._cumulative_pnl = new_pnl   # snapshot of total, not additive
        if self._last_scale_pnl is None:
            self._last_scale_pnl = new_pnl  # bootstrap: no delta on first call
        delta = new_pnl - self._last_scale_pnl

        if delta >= self.scale_up_milestone:
            old = self._scale_factor
            self._scale_factor = min(self._scale_factor * self.scale_up_factor, 5.0)
            self._last_scale_pnl = new_pnl
            logger.info(
                f"Scale UP: PnL +${delta:.2f} hit milestone | "
                f"factor {old:.2f} → {self._scale_factor:.2f}"
            )
        elif delta <= -self.scale_down_milestone:
            old = self._scale_factor
            self._scale_factor = max(self._scale_factor * self.scale_down_factor, 0.1)
            self._last_scale_pnl = new_pnl
            logger.info(
                f"Scale DOWN: PnL ${delta:.2f} hit loss milestone | "
                f"factor {old:.2f} → {self._scale_factor:.2f}"
            )

        return self.current_size

    @property
    def current_size(self) -> float:
        raw = self.base_size * self._scale_factor
        return round(max(self.min_size, min(raw, self.max_size)), 2)

    @property
    def scale_factor(self) -> float:
        return self._scale_factor
