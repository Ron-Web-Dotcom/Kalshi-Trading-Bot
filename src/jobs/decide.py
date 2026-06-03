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
                spent, tcfg.daily_ai_budget,
            )
            return None

    engine = AIDecisionEngine(db=db)
    decision = await engine.decide(market, signals)

    ticker    = market.get("ticker", "?")
    conf      = decision.confidence
    action    = decision.action
    side      = decision.side or "yes"
    net_ev    = decision.net_ev
    true_prob = decision.true_prob
    min_conf  = settings.trading.min_ai_confidence

    ev_str = f" EV={net_ev:+.1f}¢" if net_ev is not None else ""
    tp_str = f" P(YES)={true_prob:.0f}%" if true_prob is not None else ""

    # Record EVERY evaluation (BUY or HOLD) so best_pick() works
    try:
        from src.utils.daily_stats import stats as daily_stats
        daily_stats.record_evaluation(
            ticker=ticker,
            action=action,
            side=side,
            confidence=conf,
            net_ev=net_ev,
            true_prob=true_prob,
            reasoning=decision.reasoning,
            title=market.get("title", ""),
        )
    except Exception:
        pass

    if engine.should_trade(decision):
        logger.info(
            "✅ TRADE SIGNAL  %-30s  %s/%-3s  conf=%d%%  %s%s  %s",
            ticker, action, side.upper(), conf, tp_str, ev_str, decision.reasoning[:80],
        )
        # Record signal in daily stats tracker
        try:
            from src.utils.daily_stats import stats as daily_stats
            daily_stats.record_signal(ticker, conf, net_ev, action)
        except Exception:
            pass
        return {
            "ticker":     ticker,
            "action":     action,
            "side":       side,
            "confidence": conf,
            "reasoning":  decision.reasoning,
            "model":      decision.model,
            "true_prob":  true_prob,
            "net_ev":     net_ev,
        }

    # Log every HOLD with the reason so you can see what score each market got
    if conf >= min_conf * 0.6:
        logger.info(
            "🟡 NEAR-MISS      %-30s  HOLD/%-3s  conf=%d%%  (need %d%%)%s%s  %s",
            ticker, side.upper(), conf, min_conf, tp_str, ev_str, decision.reasoning[:80],
        )
        # Record real near-misses (AI said BUY but conf fell short) into daily stats.
        # They appear in the hourly report — no individual Discord spam.
        if action == "BUY" and decision.model != "rule_based" and net_ev is not None:
            try:
                from src.utils.daily_stats import stats as daily_stats
                daily_stats.record_near_miss(
                    ticker=ticker,
                    title=market.get("title", ""),
                    side=side,
                    confidence=conf,
                    net_ev=net_ev,
                    true_prob=true_prob,
                    reasoning=decision.reasoning,
                    platform=market.get("platform", "kalshi"),
                )
            except Exception:
                pass
    else:
        logger.debug(
            "⬜ HOLD           %-30s  conf=%d%%  (need %d%%)%s%s  %s",
            ticker, conf, min_conf, tp_str, ev_str, decision.reasoning[:80],
        )
    return None
