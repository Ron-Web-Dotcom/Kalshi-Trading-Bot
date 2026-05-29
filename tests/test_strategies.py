"""Tests for strategy modules."""

import pytest
from src.strategies.category_scorer import CategoryScorer
from src.strategies.portfolio_enforcer import PortfolioEnforcer
from src.strategies.safe_compounder import SafeCompounder


class TestCategoryScorer:
    def test_known_category(self):
        scorer = CategoryScorer()
        assert scorer.score("Economics") == 80

    def test_unknown_category_defaults_50(self):
        scorer = CategoryScorer()
        assert scorer.score("Underwater Basket Weaving") == 50.0

    def test_all_scores_returns_dict(self):
        scorer = CategoryScorer()
        scores = scorer.all_scores()
        assert isinstance(scores, dict)
        assert len(scores) > 0

    @pytest.mark.asyncio
    async def test_initialize_is_noop(self):
        scorer = CategoryScorer()
        await scorer.initialize()  # should not raise

    @pytest.mark.asyncio
    async def test_get_all_scores_async(self):
        scorer = CategoryScorer()
        scores = await scorer.get_all_scores()
        assert "Economics" in scores

    def test_format_scores_table(self):
        scorer = CategoryScorer()
        table = scorer.format_scores_table({"Politics": 70, "Sports": 60})
        assert "Politics" in table
        assert "Sports" in table
        assert "CATEGORY SCORES" in table


class TestPortfolioEnforcer:
    def test_allows_trade_within_limit(self):
        enforcer = PortfolioEnforcer(max_sector_pct=30.0, portfolio_value=1000.0)
        allowed, reason = enforcer.check("Politics", 50.0, [])
        assert allowed is True

    def test_blocks_trade_over_limit(self):
        enforcer = PortfolioEnforcer(max_sector_pct=30.0, portfolio_value=1000.0)
        allowed, reason = enforcer.check("Politics", 400.0, [])
        assert allowed is False
        assert "Politics" in reason


class TestSafeCompounder:
    def test_find_opportunities_empty(self):
        sc = SafeCompounder()
        opps = sc.find_opportunities([])
        assert opps == []

    def test_find_opportunity_when_edge_exists(self):
        sc = SafeCompounder(min_edge_pct=3.0, min_volume=10)
        markets = [{"ticker": "X", "title": "T", "yes_ask": 55, "no_ask": 55, "volume": 100}]
        opps = sc.find_opportunities(markets)
        assert len(opps) == 1
        assert opps[0]["edge_pct"] == 10.0

    def test_ignores_low_volume(self):
        sc = SafeCompounder(min_edge_pct=3.0, min_volume=100)
        markets = [{"ticker": "Y", "title": "T", "yes_ask": 55, "no_ask": 55, "volume": 10}]
        opps = sc.find_opportunities(markets)
        assert opps == []

    def test_ignores_fair_priced(self):
        sc = SafeCompounder(min_edge_pct=3.0, min_volume=10)
        markets = [{"ticker": "Z", "title": "T", "yes_ask": 50, "no_ask": 50, "volume": 100}]
        opps = sc.find_opportunities(markets)
        assert opps == []

    def test_sorted_by_edge_descending(self):
        sc = SafeCompounder(min_edge_pct=3.0, min_volume=10)
        markets = [
            {"ticker": "A", "title": "T", "yes_ask": 60, "no_ask": 50, "volume": 100},
            {"ticker": "B", "title": "T", "yes_ask": 55, "no_ask": 55, "volume": 100},
        ]
        opps = sc.find_opportunities(markets)
        assert opps[0]["edge_pct"] >= opps[-1]["edge_pct"]
