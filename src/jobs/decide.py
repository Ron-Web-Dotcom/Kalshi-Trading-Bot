"""Job: run AI + arbitrage decision logic on markets."""

import logging
import smtplib
import os
from datetime import date
from email.mime.text import MIMEText
from typing import Dict, List, Optional

logger = logging.getLogger("trading.jobs.decide")

# One-time flag: send the hard-cap alert only once per calendar day
_cap_alerted_date: Optional[str] = None


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


def _send_cap_email(spent: float, cap: float) -> None:
    """Send a one-time email when the hard cap is hit. Fails silently if not configured."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    alert_to  = os.environ.get("ALERT_EMAIL", "ront.devops@gmail.com")

    if not smtp_host or not smtp_user:
        return

    try:
        msg = MIMEText(
            f"Your Kalshi trading bot AI spend has hit the hard cap.\n\n"
            f"Spent today: ${spent:.2f}\n"
            f"Hard cap:    ${cap:.2f}\n\n"
            f"AI calls are paused for the rest of today.\n"
            f"Scanning and tracking continue. Everything resets at midnight."
        )
        msg["Subject"] = f"🤖💸 AI Budget Hard Cap Hit — ${spent:.2f} / ${cap:.2f}"
        msg["From"]    = smtp_user
        msg["To"]      = alert_to

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        logger.info("AI cap email sent to %s", alert_to)
    except Exception as e:
        logger.warning("AI cap email failed (non-critical): %s", e)


async def _fire_cap_alert(spent: float, cap: float) -> None:
    """Discord + email, one-time per day."""
    global _cap_alerted_date
    today = date.today().isoformat()
    if _cap_alerted_date == today:
        return  # already alerted today

    _cap_alerted_date = today
    logger.warning("AI hard cap $%.2f reached — firing one-time alert", cap)

    try:
        from src.alerts.discord import DiscordAlerter
        await DiscordAlerter().ai_budget_cap_hit(spent, cap)
    except Exception as e:
        logger.warning("Discord cap alert failed: %s", e)

    try:
        _send_cap_email(spent, cap)
    except Exception:
        pass


async def make_decision_for_market(market: Dict, signals: List[Dict], db=None) -> Optional[Dict]:
    """
    Run AI decision engine on a single market + signal context.
    Returns decision dict with action, confidence, reasoning, or None.
    """
    from src.ai.decision import AIDecisionEngine
    from src.config.settings import settings

    # AI cost budget gate
    tcfg = settings.trading
    if tcfg.enable_daily_cost_limiting:
        spent = await _get_daily_ai_spend(db)
        hard_cap = getattr(tcfg, "daily_ai_hard_cap", 15.0)
        if spent >= hard_cap:
            await _fire_cap_alert(spent, hard_cap)
            return None
        elif spent >= tcfg.daily_ai_budget:
            logger.info(
                "AI spend $%.2f past soft budget $%.2f (hard cap $%.2f) — continuing",
                spent, tcfg.daily_ai_budget, hard_cap,
            )

    engine = AIDecisionEngine(db=db)
    try:
        decision = await engine.decide(market, signals)
    except Exception as _api_err:
        err_str = str(_api_err)
        if "credit balance" in err_str or "insufficient" in err_str.lower() or "402" in err_str:
            logger.error("OpenAI API quota exhausted — check billing at platform.openai.com/account/billing")
            return None
        raise

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
            platform=market.get("platform", "kalshi"),
            close_time=market.get("close_time", "") or market.get("expiration_time", ""),
        )
    except Exception:
        pass

    if engine.should_trade(decision):
        logger.info(
            "✅ TRADE SIGNAL  %-30s  %s/%-3s  conf=%d%%  %s%s  %s",
            ticker, action, side.upper(), conf, tp_str, ev_str, decision.reasoning[:80],
        )
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
        if action == "BUY" and decision.model != "rule_based":
            try:
                from src.utils.daily_stats import stats as daily_stats
                skip_reason = (
                    f"conf {conf:.0f}% < {min_conf:.0f}% required"
                    if conf < min_conf
                    else f"EV {net_ev:+.1f}¢ below minimum"
                    if net_ev is not None and net_ev <= 0
                    else "no positive EV computed"
                    if net_ev is None
                    else "below profit gate"
                )
                daily_stats.record_near_miss(
                    ticker=ticker,
                    title=market.get("title", ""),
                    side=side,
                    confidence=conf,
                    net_ev=net_ev,
                    true_prob=true_prob,
                    reasoning=decision.reasoning,
                    platform=market.get("platform", "kalshi"),
                    skip_reason=skip_reason,
                )
            except Exception:
                pass
    else:
        logger.debug(
            "⬜ HOLD           %-30s  conf=%d%%  (need %d%%)%s%s  %s",
            ticker, conf, min_conf, tp_str, ev_str, decision.reasoning[:80],
        )
    return None
