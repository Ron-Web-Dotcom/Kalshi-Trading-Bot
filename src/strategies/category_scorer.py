"""Category scorer — rate market categories 0-100 for allocation."""

import logging
from typing import Dict

logger = logging.getLogger("trading.category_scorer")

# Default scores per Kalshi category (0 = avoid, 100 = max allocation)
DEFAULT_SCORES: Dict[str, float] = {
    "Politics": 70,
    "Economics": 80,
    "Sports": 60,
    "Science": 75,
    "Entertainment": 50,
    "Crypto": 65,
    "Weather": 55,
    "Finance": 80,
    "Technology": 75,
    "Health": 70,
    "": 50,  # unknown category
}


class CategoryScorer:
    def __init__(self, scores: Dict[str, float] = None):
        self.scores = scores or DEFAULT_SCORES

    async def initialize(self) -> None:
        pass  # No async setup needed

    def score(self, category: str) -> float:
        return self.scores.get(category, 50.0)

    def all_scores(self) -> Dict[str, float]:
        return dict(self.scores)

    async def get_all_scores(self) -> Dict[str, float]:
        return self.all_scores()

    def format_scores_table(self, scores: Dict[str, float]) -> str:
        lines = [
            "=" * 48,
            "  CATEGORY SCORES",
            "=" * 48,
            f"  {'Category':<22} {'Score':>6}  {'Alloc %':>8}",
            f"  {'-'*22} {'-'*6}  {'-'*8}",
        ]
        for cat, score in sorted(scores.items(), key=lambda x: -x[1]):
            alloc = f"{score * 0.3:.1f}%"
            lines.append(f"  {cat:<22} {score:>6.0f}  {alloc:>8}")
        lines.append("=" * 48)
        return "\n".join(lines)
