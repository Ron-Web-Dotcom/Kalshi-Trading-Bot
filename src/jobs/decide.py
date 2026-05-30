"""Job: run AI + arbitrage decision logic on markets."""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("trading.jobs.decide")


async def make_decision_for_market(market: Dict, signals: List[Dict], db=None) -> Optional[Dict]:
    """
    Run AI decision engine on a single market + signal context.
    Returns decision dict with action, confidence, reasoning, or None.
    """
    from src.ai.decision import AIDecisionEngine
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
