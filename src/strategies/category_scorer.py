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

    def score(self, category: str) -> float:
        return self.scores.get(category, 50.0)

    def all_scores(self) -> Dict[str, float]:
        return dict(self.scores)
