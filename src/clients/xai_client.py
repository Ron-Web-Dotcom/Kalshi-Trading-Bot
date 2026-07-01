"""XAI/OpenRouter client stub — kept for backward compatibility with beast_mode_bot.py."""

import logging
from dataclasses import dataclass

logger = logging.getLogger("trading.xai_client")


@dataclass
class DailyTracker:
    total_cost: float = 0.0
    daily_limit: float = 10.0
    request_count: int = 0


class XAIClient:
    """
    Legacy client name. Now routes through the AI decision engine.
    Kept as a thin wrapper so beast_mode_bot.py imports still work.
    """

    def __init__(self, db_manager=None):
        from src.config.settings import settings
        self.db_manager = db_manager
        self.daily_tracker = DailyTracker(
            daily_limit=settings.trading.daily_ai_budget
        )

    async def close(self):
        pass
