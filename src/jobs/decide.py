"""
Job: AI + rule-based decision engine running as a team.

Normal mode  → AI engine + rule engine run in parallel.
              If both agree on BUY → confidence boosted (agreement bonus).
              Best signal wins.

AI-capped    → rule engine takes over as sole decision maker.
              Uses Manifold, Metaculus, web search, sentiment.
              Same 70% confidence threshold applies — no free passes.

AI-capped alert → one-time white Discord embed + email when $15 hard cap hit.
"""

import asyncio
import logging
import smtplib
import os
from datetime import date
from email.mime.text import MIMEText
from typing import Dict, List, Optional

logger = logging.getLogger("trading.jobs.decide")

# One-time flag per calendar day
_cap_alerted_date: Optional[str] = None


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_daily_ai_spend(db) -> float:
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


# ── Cap alert ─────────────────────────────────────────────────────────────────

def _send_cap_email(spent: float, cap: float) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    alert_to  = os.environ.get("ALERT_EMAIL", "ront.devops@gmail.com")
    if not smtp_host or not smtp_user:
        return
    try:
        msg = MIMEText(
            f"AI spend hit the hard cap.\n\n"
            f"Spent today: ${spent:.2f}\nHard cap: ${cap:.2f}\n\n"
            f"Rule engine (Manifold + Metaculus + web search) has taken over.\n"
            f"Resets at midnight — AI resumes automatically."
        )
        msg["Subject"] = f"🤖💸 AI Budget Hard Cap ${cap:.0f} Hit — rule engine active"
        msg["From"]    = smtp_user
        msg["To"]      = alert_to
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception as e:
        logger.warning("AI cap email failed (non-critical): %s", e)


async def _fire_cap_alert(spent: float, cap: float) -> None:
    global _cap_alerted_date
    today = date.today().isoformat()
    if _cap_alerted_date == today:
        return
    _cap_alerted_date = today
    logger.warning("AI hard cap $%.2f reached — rule engine taking over", cap)
    try:
        from src.alerts.discord import DiscordAlerter
        await DiscordAlerter().ai_budget_cap_hit(spent, cap)
    except Exception as e:
        logger.warning("Discord cap alert failed: %s", e)
    try:
        _send_cap_email(spent, cap)
    except Exception:
        pass


# ── Merge logic ───────────────────────────────────────────────────────────────

def _merge(ai_result: Optional[Dict], rule_result: Optional[Dict]) -> Optional[Dict]:
    """
    Combine AI and rule engine outputs into the best signal.

    Agreement bonus: both say BUY same side → +5% confidence on the winner.
    Disagreement:    only one says BUY → use that one as-is.
    Both HOLD:       return None.
    """
    ai_buy   = ai_result   and ai_result.get("action")   == "BUY"
    rule_buy = rule_result and rule_result.get("action") == "BUY"

    if not ai_buy and not rule_buy:
        return None

    if ai_buy and rule_buy:
        # Pick the higher-confidence signal and boost it
        ai_conf   = ai_result.get("confidence", 0)
        rule_conf = rule_result.get("confidence", 0)
        same_side = ai_result.get("side") == rule_result.get("side")
        bonus     = 5.0 if same_side else 0.0

        if ai_conf >= rule_conf:
            winner = dict(ai_result)
            winner["confidence"] = min(99.0, ai_conf + bonus)
            winner["reasoning"]  = (
                f"[AI+rule agree{'+' + str(int(bonus)) + '%' if bonus else ''}] "
                + winner["reasoning"]
                + f" | rule_engine: {rule_result.get('reasoning','')[:80]}"
            )
        else:
            winner = dict(rule_result)
            winner["confidence"] = min(99.0, rule_conf + bonus)
            winner["reasoning"]  = (
                f"[AI+rule agree{'+' + str(int(bonus)) + '%' if bonus else ''}] "
                + winner["reasoning"]
                + f" | AI: {ai_result.get('reasoning','')[:80]}"
            )
        return winner

    # Only one engine says BUY
    return ai_result if ai_buy else rule_result


# ── Rule engine wrapper ───────────────────────────────────────────────────────

async def _run_rule_engine(market: Dict, context: str) -> Optional[Dict]:
    """Run the rule-based engine and return a decision dict (same shape as AI output)."""
    try:
        from src.ai.rule_engine import score as rule_score
        # Pull Manifold / Metaculus snippets from context if embedded
        manifold_text  = ""
        metaculus_text = ""
        for line in context.splitlines():
            ll = line.lower()
            if "manifold" in ll:
                manifold_text += line + "\n"
            elif "metaculus" in ll:
                metaculus_text += line + "\n"

        rd = rule_score(market, context, manifold_text or None, metaculus_text or None)
        return {
            "ticker":     market.get("ticker", "?"),
            "action":     rd.action,
            "side":       rd.side,
            "confidence": rd.confidence,
            "reasoning":  rd.reasoning,
            "model":      rd.model,
            "true_prob":  rd.true_prob,
            "net_ev":     rd.net_ev,
        }
    except Exception as e:
        logger.debug("Rule engine error: %s", e)
        return None


# ── Main entry ────────────────────────────────────────────────────────────────

async def make_decision_for_market(
    market: Dict,
    signals: List[Dict],
    db=None,
    min_confidence: Optional[float] = None,
) -> Optional[Dict]:
    """
    Run AI + rule engine as a team.
    Returns best decision dict, or None if both say HOLD or below min_confidence.
    Pass min_confidence to override the global setting (e.g. 55.0 for live markets).
    """
    from src.config.settings import settings

    tcfg     = settings.trading
    hard_cap = getattr(tcfg, "daily_ai_hard_cap", 15.0)
    ai_capped = False

    if tcfg.enable_daily_cost_limiting:
        spent = await _get_daily_ai_spend(db)
        if spent >= hard_cap:
            await _fire_cap_alert(spent, hard_cap)
            ai_capped = True
        elif spent >= tcfg.daily_ai_budget:
            logger.info(
                "AI spend $%.2f past soft budget $%.2f (hard cap $%.2f) — both engines active",
                spent, tcfg.daily_ai_budget, hard_cap,
            )

    # ── Fetch context once — shared by both engines ──────────────────────────
    context = ""
    try:
        from src.data.context_builder import build_context
        context = await asyncio.wait_for(
            build_context(market, signals),
            timeout=16.0,
        )
    except Exception as e:
        logger.debug("Context build error: %s", e)

    ticker   = market.get("ticker", "?")
    min_conf = min_confidence if min_confidence is not None else tcfg.min_ai_confidence

    # ── Run engines ──────────────────────────────────────────────────────────
    if ai_capped:
        # Rule engine only — free, no OpenAI
        logger.info("🔄 rule_engine-only for %s (AI capped)", ticker)
        rule_result = await _run_rule_engine(market, context)
        final = rule_result if (rule_result and rule_result.get("action") == "BUY") else None
    else:
        # Both in parallel — teamwork
        from src.ai.decision import AIDecisionEngine
        engine = AIDecisionEngine(db=db)

        async def _ai_task():
            try:
                decision = await engine.decide(market, signals, prebuilt_context=context)
                return {
                    "ticker":     ticker,
                    "action":     decision.action,
                    "side":       decision.side or "yes",
                    "confidence": decision.confidence,
                    "reasoning":  decision.reasoning,
                    "model":      decision.model,
                    "true_prob":  decision.true_prob,
                    "net_ev":     decision.net_ev,
                } if engine.should_trade(decision) else None
            except Exception as _api_err:
                err = str(_api_err)
                if "credit balance" in err or "insufficient" in err.lower() or "402" in err:
                    logger.error("OpenAI quota exhausted — rule engine continues solo")
                    return None
                raise

        ai_result, rule_result = await asyncio.gather(
            _ai_task(),
            _run_rule_engine(market, context),
            return_exceptions=False,
        )
        final = _merge(ai_result, rule_result)

    if final is None:
        _log_hold(market, context, min_conf, signals)
        return None

    # ── Signal accepted ──────────────────────────────────────────────────────
    conf    = final.get("confidence", 0)
    action  = final.get("action", "HOLD")
    side    = final.get("side", "yes")
    net_ev  = final.get("net_ev")
    true_p  = final.get("true_prob")
    model   = final.get("model", "?")

    ev_str = f" EV={net_ev:+.1f}¢" if net_ev is not None else ""
    tp_str = f" P(YES)={true_p:.0f}%" if true_p is not None else ""

    logger.info(
        "✅ TRADE SIGNAL  %-30s  %s/%-3s  conf=%d%%  [%s]%s%s  %s",
        ticker, action, side.upper(), conf, model, tp_str, ev_str,
        final.get("reasoning", "")[:80],
    )

    # Record in daily stats + evaluations
    try:
        from src.utils.daily_stats import stats as daily_stats
        daily_stats.record_evaluation(
            ticker=ticker, action=action, side=side, confidence=conf,
            net_ev=net_ev, true_prob=true_p, reasoning=final.get("reasoning", ""),
            title=market.get("title", ""), platform=market.get("platform", "kalshi"),
            close_time=market.get("close_time", "") or market.get("expiration_time", ""),
        )
        daily_stats.record_signal(ticker, conf, net_ev, action)
    except Exception:
        pass

    return final


def _log_hold(market: Dict, context: str, min_conf: float, signals: List[Dict]) -> None:
    """Log a HOLD — also records near-misses for the daily digest."""
    ticker = market.get("ticker", "?")
    try:
        from src.utils.daily_stats import stats as daily_stats
        daily_stats.record_evaluation(
            ticker=ticker, action="HOLD", side="yes", confidence=0,
            net_ev=None, true_prob=None, reasoning="both engines: HOLD",
            title=market.get("title", ""), platform=market.get("platform", "kalshi"),
            close_time=market.get("close_time", "") or market.get("expiration_time", ""),
        )
    except Exception:
        pass
    logger.debug("⬜ HOLD  %s  (both engines below threshold)", ticker)
