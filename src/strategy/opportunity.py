"""
Best-single-opportunity hunter — scans BOTH Kalshi and Polymarket.

Cost-efficient pipeline:
  1. Rule-based pre-score ALL candidates (free — no API calls)
  2. Call AI only on the TOP 3 by pre-score
  3. Return the single best AI-confirmed trade

This cuts AI calls from 200/cycle to 3/cycle — ~98% cost reduction.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.opportunity")


def _pre_score(market: Dict, poly_comp: Optional[Dict] = None) -> float:
    """
    Fast rule-based pre-score with NO API calls.
    Used to rank all candidates before paying for AI evaluation.
    Higher = more promising, evaluate with AI first.
    """
    yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
    no_ask  = float(market.get("no_ask")  or (100 - yes_ask) if yes_ask else 0)
    volume  = float(market.get("volume") or 0)

    if yes_ask <= 0 or yes_ask >= 100:
        return 0.0

    # Prefer markets near 50¢ — most room for edge on either side
    price_score = 1.0 - abs(yes_ask - 50) / 50.0

    # Liquidity — more volume = more confidence in price signal
    liquidity = min(volume / 1000.0, 1.0)

    # Cross-platform bonus — Polymarket comparison available
    cross_bonus = 1.3 if poly_comp else 1.0

    # Time bonus — closing soon = more actionable
    time_bonus = 1.0
    ct = market.get("close_time", "")
    if ct:
        try:
            close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            hours = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours <= 6:
                time_bonus = 1.5
            elif 0 < hours <= 48:
                time_bonus = 1.2
        except Exception:
            pass

    return price_score * max(liquidity, 0.1) * cross_bonus * time_bonus


def score_opportunity(
    market:    Dict,
    decision:  Dict,
    poly_comp: Optional[Dict] = None,
) -> float:
    """Post-AI score using actual AI confidence and EV."""
    net_ev     = decision.get("net_ev") or 0.0
    confidence = decision.get("confidence", 0.0)
    volume     = market.get("volume", 0)

    if net_ev <= 0 or confidence <= 0:
        return 0.0

    ev_score         = min(net_ev / 10.0, 1.0)
    confidence_score = confidence / 100.0
    liquidity_score  = max(min(volume / 100.0, 1.0), 0.1)
    data_quality     = 1.0 if poly_comp else 0.85
    base_score       = ev_score * confidence_score * data_quality * liquidity_score

    time_bonus = 1.0
    ct = market.get("close_time") or market.get("expiration_time")
    if ct:
        try:
            close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            hours = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours <= 6:
                time_bonus = 1.5
            elif 0 < hours <= 24:
                time_bonus = 1.3
        except Exception:
            pass

    return base_score * time_bonus


class OpportunityHunter:
    """
    Two-stage pipeline: rule-based pre-filter → AI evaluation on top N only.
    Reduces AI API calls from O(all_candidates) to O(3) per cycle.
    """

    def __init__(self, db=None, ai_top_n: int = 3):
        self.db = db
        self.ai_top_n = ai_top_n  # how many candidates to send to AI

    async def find_best(
        self,
        markets:       List[Dict],
        arb_signals:   List[Dict],
        poly_comps:    List[Dict],
        min_score:     float = 0.05,
        poly_markets:  Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        from src.jobs.decide import make_decision_for_market

        all_candidates = list(markets)
        if poly_markets:
            all_candidates.extend(poly_markets)

        poly_by_ticker = {c["kalshi_ticker"]: c for c in poly_comps}

        logger.info(
            "── Opportunity Hunt (%d Kalshi + %d Polymarket = %d total) ──",
            len(markets), len(poly_markets) if poly_markets else 0, len(all_candidates),
        )

        # ── Stage 1: rule-based pre-score (FREE — no AI calls) ───────────────
        prescored = []
        for market in all_candidates:
            yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
            title   = market.get("title", "")

            if yes_ask <= 1 or yes_ask >= 99:
                continue
            if not title or len(title) < 10 or title.startswith("0x"):
                continue

            poly_comp  = poly_by_ticker.get(market.get("ticker", ""))
            pre        = _pre_score(market, poly_comp)
            prescored.append((pre, market, poly_comp))

        # Sort by pre-score desc, take top N for AI evaluation
        prescored.sort(key=lambda x: x[0], reverse=True)
        top_candidates = prescored[:self.ai_top_n]

        logger.info(
            "Pre-scored %d candidates — sending top %d to AI",
            len(prescored), len(top_candidates),
        )

        # ── Stage 2: AI evaluation on top N only ─────────────────────────────
        best_score  = 0.0
        best_result = None

        for pre_score, market, poly_comp in top_candidates:
            ticker = market.get("ticker", "")

            enriched = dict(market)
            if poly_comp:
                enriched["poly_yes"]      = poly_comp["poly_yes"]
                enriched["poly_no"]       = poly_comp["poly_no"]
                enriched["poly_question"] = poly_comp["poly_question"]

            decision = await make_decision_for_market(enriched, arb_signals, db=self.db)
            if not decision:
                continue

            score = score_opportunity(market, decision, poly_comp)
            yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
            no_ask  = float(market.get("no_ask")  or (100 - yes_ask))

            logger.info(
                "  [AI] %-32s pre=%.2f score=%.3f | conf=%d%% ev=%.1f¢ | %s",
                ticker, pre_score, score,
                decision.get("confidence", 0),
                decision.get("net_ev") or 0,
                decision.get("reasoning", "")[:60],
            )

            if score > best_score:
                best_score = score
                side        = decision.get("side", "yes")
                price_cents = yes_ask if side == "yes" else no_ask
                best_result = {
                    "market":      market,
                    "decision":    decision,
                    "poly_comp":   poly_comp,
                    "score":       score,
                    "side":        side,
                    "price_cents": price_cents,
                    "platform":    market.get("platform", "kalshi"),
                }

        if best_result and best_score >= min_score:
            m = best_result["market"]
            d = best_result["decision"]
            logger.info(
                "BEST [%s]: %s BUY %s @ %.1f¢ | score=%.3f conf=%d%% EV=%.1f¢",
                best_result["platform"].upper(), m.get("ticker"),
                d.get("side", "yes").upper(), best_result["price_cents"],
                best_score, d.get("confidence", 0), d.get("net_ev") or 0,
            )
            return best_result

        logger.info("No opportunity cleared min_score=%.2f — sitting out", min_score)
        return None
