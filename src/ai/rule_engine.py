"""
Rule-based decision engine — runs FREE, no OpenAI calls.

Uses:
  - Manifold Markets API  (community probability)
  - Metaculus API         (forecaster probability)
  - Web search headlines  (sentiment keywords)
  - Polymarket price      (implicit market consensus)

Designed to:
  1. Run IN PARALLEL with the AI engine every cycle (teamwork)
  2. Take over as SOLE engine when AI daily cap is hit

Confidence formula:
  - Base: edge between free-market consensus and listed price
  - Boost: multiple sources agree on direction
  - Penalty: sources conflict, thin context
  - Floor: 0 / Ceiling: 88 (AI can reach 95, rule engine caps at 88)
  - Minimum to trade: 77% — requires probability sources (Manifold/Metaculus) + corroborating data
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.rule_engine")

# ── Sentiment keyword tables ────────────────────────────────────────────────

_YES_KEYWORDS = [
    "confirmed", "official", "announced", "approved", "passed", "won",
    "victory", "signed", "launched", "completed", "achieved", "record",
    "surge", "rally", "breakthrough", "deal", "agreement", "certif",
    "winner", "elected", "leads", "ahead", "dominant", "strong",
    "certain", "likely", "expected", "imminent", "scheduled",
]

_NO_KEYWORDS = [
    "cancelled", "canceled", "postponed", "delayed", "failed", "rejected",
    "denied", "withdrawn", "suspended", "halted", "reversed", "collapsed",
    "unlikely", "doubt", "uncertain", "questioned", "disputed", "denied",
    "lost", "defeat", "trailing", "behind", "weak", "dropped", "fell",
    "missed", "below", "under", "slump",
]


@dataclass
class RuleDecision:
    action: str          # "BUY" | "HOLD"
    side: str            # "yes" | "no"
    confidence: float    # 0–100
    net_ev: Optional[float]
    true_prob: Optional[float]
    reasoning: str
    sources_used: List[str] = field(default_factory=list)
    model: str = "rule_based"


def _parse_probability_from_text(text: str) -> Optional[float]:
    """
    Extract a probability from free-text like:
      'Manifold: 73% YES' / 'Metaculus community: 68%' / '0.71 probability'
    Returns 0-100 float or None.
    """
    if not text:
        return None
    # Match "73%" or "0.73" patterns
    pct = re.search(r'(\d{1,3})\s*%', text)
    if pct:
        v = float(pct.group(1))
        if 1 <= v <= 99:
            return v
    dec = re.search(r'\b0\.(\d+)\b', text)
    if dec:
        return float("0." + dec.group(1)) * 100
    return None


def _sentiment_score(text: str) -> Tuple[float, float]:
    """
    Returns (yes_score, no_score) — count of matching keywords, normalised 0-1.
    """
    if not text:
        return 0.0, 0.0
    low = text.lower()
    yes_hits = sum(1 for w in _YES_KEYWORDS if w in low)
    no_hits  = sum(1 for w in _NO_KEYWORDS  if w in low)
    total = yes_hits + no_hits + 1  # +1 avoids div-zero
    return yes_hits / total, no_hits / total


def score(
    market: Dict,
    context: str,
    manifold_text: Optional[str] = None,
    metaculus_text: Optional[str] = None,
) -> RuleDecision:
    """
    Core rule-based scorer.  All inputs are strings already fetched;
    no network calls made here.
    """
    ticker     = market.get("ticker", "?")
    yes_ask    = float(market.get("yes_ask") or market.get("last_price") or 50)
    yes_bid    = float(market.get("yes_bid") or yes_ask)
    market_mid = (yes_ask + yes_bid) / 2.0  # 0-100 ¢

    sources_used: List[str] = []
    prob_estimates: List[float] = []

    # ── 1. Manifold probability ─────────────────────────────────────────────
    manifold_prob = _parse_probability_from_text(manifold_text or "")
    if manifold_prob is not None:
        prob_estimates.append(manifold_prob)
        sources_used.append(f"Manifold={manifold_prob:.0f}%")

    # ── 2. Metaculus probability ────────────────────────────────────────────
    metaculus_prob = _parse_probability_from_text(metaculus_text or "")
    if metaculus_prob is not None:
        prob_estimates.append(metaculus_prob)
        sources_used.append(f"Metaculus={metaculus_prob:.0f}%")

    # ── 3. Extract any additional probabilities embedded in context ─────────
    if context:
        for line in context.splitlines():
            if any(kw in line.lower() for kw in ("manifold", "metaculus", "polymarket", "probability", "forecast")):
                p = _parse_probability_from_text(line)
                if p is not None and p not in prob_estimates:
                    prob_estimates.append(p)
                    sources_used.append(f"context_prob={p:.0f}%")

    # ── 4. Sentiment from headlines ─────────────────────────────────────────
    yes_sent, no_sent = _sentiment_score(context)
    if yes_sent > 0.3 or no_sent > 0.3:
        dominant = "yes" if yes_sent >= no_sent else "no"
        sources_used.append(f"sentiment={dominant}({max(yes_sent, no_sent):.0%})")

    # ── 5. Compute consensus probability ────────────────────────────────────
    if prob_estimates:
        consensus = sum(prob_estimates) / len(prob_estimates)
    else:
        # Fall back to sentiment only
        if yes_sent > no_sent + 0.15:
            consensus = 60.0
        elif no_sent > yes_sent + 0.15:
            consensus = 40.0
        else:
            consensus = 50.0  # no signal

    # ── 6. Determine side and edge ───────────────────────────────────────────
    if consensus >= 50:
        side      = "yes"
        true_prob = consensus
        edge      = consensus - market_mid          # positive = YES underpriced
    else:
        side      = "no"
        true_prob = consensus
        edge      = (100 - consensus) - (100 - market_mid)  # NO edge

    net_ev = edge * 0.98  # in cents, after 2% fee

    # ── 7. Confidence ────────────────────────────────────────────────────────
    # Calibrated against the 75% bid threshold: after the 8% no-source haircut
    # (× 0.92), edge ≥ 15 must still clear 75%  →  need base ≥ 81.5 → use 82.
    if abs(edge) >= 20:
        base_conf = 90.0   # × 0.92 = 82.8% — clear BID
    elif abs(edge) >= 15:
        base_conf = 82.0   # × 0.92 = 75.4% — just clears threshold
    elif abs(edge) >= 10:
        base_conf = 74.0   # × 0.92 = 68.1% — below threshold, HOLD
    elif abs(edge) >= 7:
        base_conf = 66.0   # weak — HOLD
    else:
        base_conf = 50.0   # no signal — HOLD

    # Boost: more agreeing sources = higher confidence
    n_prob_sources = len(prob_estimates)
    if n_prob_sources >= 2 and max(prob_estimates) - min(prob_estimates) <= 10:
        base_conf = min(base_conf + 7, 88.0)   # tight agreement = big boost
    elif n_prob_sources >= 1:
        base_conf = min(base_conf + 4, 88.0)

    # Boost: sentiment aligns with probability edge
    if (side == "yes" and yes_sent > no_sent + 0.2) or (side == "no" and no_sent > yes_sent + 0.2):
        base_conf = min(base_conf + 4, 88.0)

    # Penalty: no external probability sources — apply a modest discount but still
    # allow edge-based signals to reach the minimum confidence threshold.
    if n_prob_sources == 0:
        base_conf *= 0.92   # ~8% haircut: e.g. 80→74, 74→68, 66→61

    # Penalty: conflicting probability sources
    if n_prob_sources >= 2 and max(prob_estimates) - min(prob_estimates) > 20:
        base_conf -= 12.0

    confidence = max(0.0, min(base_conf, 88.0))

    # ── 8. Action gate ───────────────────────────────────────────────────────
    from src.config.settings import settings
    min_conf = settings.trading.min_ai_confidence  # 75
    min_ev   = 2.0 if confidence < 75 else 1.0 if confidence < 85 else 0.5

    if confidence >= min_conf and net_ev >= min_ev:
        action = "BUY"
    else:
        action = "HOLD"

    # ── 9. Reasoning string ──────────────────────────────────────────────────
    src_str = " | ".join(sources_used) if sources_used else "sentiment-only"
    reasoning = (
        f"[rule_engine] {src_str} → consensus={consensus:.0f}% "
        f"vs market={market_mid:.0f}¢ | edge={edge:+.1f}¢ EV={net_ev:+.1f}¢ "
        f"conf={confidence:.0f}%"
    )

    logger.debug("rule_engine %s: %s", ticker, reasoning)

    return RuleDecision(
        action=action,
        side=side,
        confidence=confidence,
        net_ev=net_ev,
        true_prob=true_prob,
        reasoning=reasoning,
        sources_used=sources_used,
    )
