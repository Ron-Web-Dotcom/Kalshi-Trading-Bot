"""
Best-single-opportunity hunter — scans BOTH Kalshi and Polymarket.

Pulls live markets from both platforms, evaluates each with the AI using
full real-world context, scores each candidate, and returns the single
highest-confidence, highest-EV trade regardless of which platform it's on.

Philosophy:
  - Don't chase volume; chase conviction.
  - One great trade beats five mediocre ones, especially on a small account.
  - If nothing clears the bar, return nothing — cash is a valid position.
  - Platform is irrelevant; edge is everything.

Scoring formula (all components 0–1, multiplied together):
  score = ev_score × confidence_score × data_quality × liquidity_score

Where:
  ev_score        = min(net_ev_cents / 20, 1.0)   — caps at 20¢ net EV
  confidence_score= confidence / 100
  data_quality    = 1.0 if cross-platform price match exists, 0.7 otherwise
  liquidity_score = min(volume / 10000, 1.0)
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("trading.opportunity")


def score_opportunity(
    market:    Dict,
    decision:  Dict,
    poly_comp: Optional[Dict] = None,
) -> float:
    """
    Compute a 0.0–1.0 opportunity score for a market + AI decision pair.
    Higher = better trade.

    Time bonus multiplier:
      - closes within 6 h  → 1.5x
      - closes within 24 h → 1.3x
      - otherwise          → 1.0x
    """
    from datetime import datetime, timezone

    net_ev     = decision.get("net_ev") or 0.0
    confidence = decision.get("confidence", 0.0)
    volume     = market.get("volume", 0)

    if net_ev <= 0 or confidence <= 0:
        return 0.0

    ev_score         = min(net_ev / 10.0, 1.0)
    confidence_score = confidence / 100.0
    liquidity_score  = max(min(volume / 100.0, 1.0), 0.1)  # floor at 0.1 so zero-volume markets still score
    data_quality     = 1.0 if poly_comp else 0.85

    base_score = ev_score * confidence_score * data_quality * liquidity_score

    # --- Time bonus: prioritise markets closing soon ---
    time_bonus = 1.0
    close_time_raw = market.get("close_time") or market.get("expiration_time")
    if close_time_raw:
        try:
            if isinstance(close_time_raw, str):
                # Accept ISO-8601 with or without timezone suffix
                close_dt = datetime.fromisoformat(
                    close_time_raw.replace("Z", "+00:00")
                )
            else:
                close_dt = close_time_raw  # already a datetime
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
            hours_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hours_left <= 6:
                time_bonus = 1.5
            elif 0 < hours_left <= 24:
                time_bonus = 1.3
        except Exception:
            pass  # unparseable — no bonus

    return base_score * time_bonus


class OpportunityHunter:
    """
    Evaluates all candidate markets with the AI, then returns the single
    best opportunity (or None if nothing clears the bar).
    """

    def __init__(self, db=None):
        self.db = db

    async def find_best(
        self,
        markets:       List[Dict],
        arb_signals:   List[Dict],
        poly_comps:    List[Dict],
        min_score:     float = 0.05,
        poly_markets:  Optional[List[Dict]] = None,
    ) -> Optional[Dict]:
        """
        Score all candidates from BOTH Kalshi and Polymarket, return the best.

        Returns a dict:
          {market, decision, poly_comp, score, side, price_cents, platform}
        or None if no market clears min_score.
        """
        from src.jobs.decide import make_decision_for_market

        # Merge Polymarket markets into candidate pool
        all_candidates = list(markets)
        if poly_markets:
            all_candidates.extend(poly_markets)
            logger.info("  Candidates: %d Kalshi + %d Polymarket = %d total",
                        len(markets), len(poly_markets), len(all_candidates))

        # Build poly cross-reference by Kalshi ticker
        poly_by_ticker = {c["kalshi_ticker"]: c for c in poly_comps}

        best_score    = 0.0
        best_result   = None
        evaluated     = 0
        skipped_score = 0

        logger.info("── Opportunity Hunt (%d total candidates) ────────────────────", len(all_candidates))

        for market in all_candidates:
            ticker  = market.get("ticker", "")
            yes_ask = market.get("yes_ask", 0)
            no_ask  = market.get("no_ask",  0)
            volume  = market.get("volume",  0)

            # Basic sanity
            if yes_ask <= 5 or yes_ask >= 95:
                continue
            # Skip markets with no readable title (e.g. raw 0x... hex conditionIds stored as title)
            title = market.get("title", "")
            if not title or len(title) < 10 or title.startswith("0x"):
                continue

            poly_comp = poly_by_ticker.get(ticker)

            # Enrich market dict with Polymarket data so AI sees both prices
            enriched = dict(market)
            if poly_comp:
                enriched["poly_yes"]      = poly_comp["poly_yes"]
                enriched["poly_no"]       = poly_comp["poly_no"]
                enriched["poly_question"] = poly_comp["poly_question"]

            decision = await make_decision_for_market(enriched, arb_signals, db=self.db)
            evaluated += 1

            if not decision:
                skipped_score += 1
                continue

            score = score_opportunity(market, decision, poly_comp)
            logger.debug(
                "  %-32s score=%.3f | conf=%d%% ev=%.1f¢ vol=%d%s",
                ticker, score,
                decision.get("confidence", 0),
                decision.get("net_ev") or 0,
                volume,
                " [POLY]" if poly_comp else "",
            )

            if score > best_score:
                best_score  = score
                side        = decision.get("side", "yes")
                price_cents = yes_ask if side == "yes" else no_ask
                platform    = market.get("platform", "kalshi")
                best_result = {
                    "market":      market,
                    "decision":    decision,
                    "poly_comp":   poly_comp,
                    "score":       score,
                    "side":        side,
                    "price_cents": price_cents,
                    "platform":    platform,
                }

        logger.info(
            "Hunt complete: evaluated=%d skipped(low score)=%d | best_score=%.3f (need %.3f)",
            evaluated, skipped_score, best_score, min_score,
        )
        if evaluated == 0:
            logger.warning(
                "No candidates were evaluated — all %d passed to hunt had score=0 or were skipped.",
                len(all_candidates),
            )

        if best_result and best_score >= min_score:
            m = best_result["market"]
            d = best_result["decision"]
            p = best_result.get("poly_comp")
            poly_str = f" | Poly_YES={p['poly_yes']:.0f}¢" if p else ""
            platform_tag = best_result["platform"].upper()
            logger.info(
                "BEST OPPORTUNITY [%s]: %s → BUY %s @ %.0f¢ | "
                "score=%.3f conf=%d%% EV=%.1f¢%s | %s",
                platform_tag,
                m.get("ticker"), d.get("side", "yes").upper(),
                best_result["price_cents"],
                best_score, d.get("confidence", 0), d.get("net_ev") or 0,
                poly_str,
                d.get("reasoning", "")[:80],
            )
            return best_result

        logger.info("No opportunity cleared the bar (min_score=%.2f) — sitting out", min_score)
        return None
