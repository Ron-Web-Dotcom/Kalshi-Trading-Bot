"""Job: run AI + arbitrage decision logic on markets."""

import logging
from datetime import date
from typing import Dict, List, Optional

logger = logging.getLogger("trading.jobs.decide")


async def _get_daily_ai_spend(db) -> float:
    """Sum AI spend recorded today from the ai_decisions table."""
    if not db:
        return 0.0
    try:
        today = date.today().isoformat()
        row = await db.fetchone(
            "SELECT COALESCE(SUM(cost_usd),0) AS total FROM ai_decisions WHERE decided_at >= ?",
            (today + "T00:00:00",)
        )
        return float((row or {}).get("total", 0))
    except Exception:
        return 0.0


async def make_decision_for_market(market: Dict, signals: List[Dict], db=None) -> Optional[Dict]:
    """
    Run AI decision engine on a single market + signal context.
    Returns decision dict with action, confidence, reasoning, or None.
    """
    from src.ai.decision import AIDecisionEngine
    from src.config.settings import settings

    # AI cost budget gate — prevent runaway API spend
    tcfg = settings.trading
    if tcfg.enable_daily_cost_limiting:
        spent = await _get_daily_ai_spend(db)
        if spent >= tcfg.daily_ai_budget:
            logger.warning(
                "Daily AI budget exhausted ($%.4f >= $%.2f) — skipping AI call",
                spent, cfg.daily_budget_usd,
            )
            return None

    engine = AIDecisionEngine(db=db)
    decision = await engine.decide(market, signals)
    if engine.should_trade(decision):
        return {
            "ticker":     market.get("ticker"),
            "action":     decision.action,
            "side":       decision.side,
            "confidence": decision.confidence,
            "reasoning":  decision.reasoning,
            "model":      decision.model,
            "true_prob":  decision.true_prob,
            "net_ev":     decision.net_ev,
        }
    return None
