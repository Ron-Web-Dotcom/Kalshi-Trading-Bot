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

    # Class-level cache: ticker → epoch_rejected — skip re-evaluating within 60 min
    _ai_rejection_cache: dict = {}
    _REJECTION_TTL_SECS: int = 3600   # 1 hour
    _REJECTION_FILE: str = "/tmp/kalshi_ai_rejections.json"

    def __init__(self, db=None, ai_top_n: int = 3):
        self.db = db
        self.ai_top_n = ai_top_n
        # Load persisted rejections on first instantiation
        if not self.__class__._ai_rejection_cache:
            self.__class__._load_rejections()

    @classmethod
    def _load_rejections(cls) -> None:
        import json, os, time
        try:
            if os.path.exists(cls._REJECTION_FILE):
                data = json.loads(open(cls._REJECTION_FILE).read())
                now = time.time()
                cls._ai_rejection_cache = {
                    k: v for k, v in data.items()
                    if now - v.get("ts", 0) < cls._REJECTION_TTL_SECS
                }
        except Exception:
            pass

    @classmethod
    def _save_rejections(cls) -> None:
        import json
        try:
            with open(cls._REJECTION_FILE, "w") as f:
                json.dump(cls._ai_rejection_cache, f)
        except Exception:
            pass

    @classmethod
    def _is_recently_rejected(cls, ticker: str) -> bool:
        import time
        entry = cls._ai_rejection_cache.get(ticker)
        if not entry:
            return False
        return (time.time() - entry.get("ts", 0)) < cls._REJECTION_TTL_SECS

    @classmethod
    def _mark_rejected(cls, ticker: str, reason: str = "") -> None:
        import time
        now = time.time()
        cls._ai_rejection_cache[ticker] = {"ts": now, "reason": reason}
        # Prune stale entries
        cls._ai_rejection_cache = {
            k: v for k, v in cls._ai_rejection_cache.items()
            if (now - v.get("ts", 0)) < cls._REJECTION_TTL_SECS
        }
        cls._save_rejections()

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

        # Long-term speculation markets — not tradeable, skip before AI
        _SKIP_PHRASES = [
            "before gta", "gta vi", "gta 6",
            "rihanna", "kanye", "playboi carti", "drake album",
            "before agi", "agi by",
            "invades taiwan", "china taiwan",
            "world war", "nuclear",
            "jesus christ", "second coming", "rapture",
            "gavin newsom", "2028 democratic", "2028 president",
            "bernie endorse", "endorse dan osborn",
            "waymo launch", "waymo nashville",
            "before 2027", "before 2028", "before 2029", "before 2030",
            "before 203", "before 204", "before 205",
        ]

        # ── Stage 1: rule-based pre-score (FREE — no AI calls) ───────────────
        prescored = []
        for market in all_candidates:
            yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
            title   = market.get("title", "")

            if yes_ask <= 1 or yes_ask >= 99:
                continue
            if not title or len(title) < 10 or title.startswith("0x"):
                continue
            # Skip obvious long-term speculation — no edge, wastes AI calls
            title_lower = title.lower()
            if any(p in title_lower for p in _SKIP_PHRASES):
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

            if self._is_recently_rejected(ticker):
                logger.debug("AI skip (cached rejection): %s", ticker[:40])
                continue

            enriched = dict(market)
            if poly_comp:
                enriched["poly_yes"]      = poly_comp["poly_yes"]
                enriched["poly_no"]       = poly_comp["poly_no"]
                enriched["poly_question"] = poly_comp["poly_question"]

            decision = await make_decision_for_market(enriched, arb_signals, db=self.db)
            if not decision:
                continue

            score = score_opportunity(market, decision, poly_comp)
            if score <= 0:
                self._mark_rejected(ticker, decision.get("reasoning", "")[:60])
                continue  # zero-score market can never be best — skip it
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

    async def find_top_live(
        self,
        live_markets:  List[Dict],
        arb_signals:   List[Dict],
        min_confidence: float = 70.0,
        top_n:          int   = 3,
        ai_eval_n:      int   = 6,
    ) -> List[Dict]:
        """
        Scan all live in-play markets, run AI on the best ai_eval_n by pre-score,
        and return up to top_n results that clear min_confidence.
        Used for the dedicated live trading pass.
        """
        from src.jobs.decide import make_decision_for_market

        if not live_markets:
            return []

        # Price momentum — detect markets where price moved significantly vs stored value
        momentum_tickers: dict = {}
        if self.db:
            try:
                for market in live_markets:
                    ticker  = market.get("ticker", "")
                    cur_ask = float(market.get("yes_ask") or 0)
                    if not cur_ask or not ticker:
                        continue
                    row = await self.db.fetchone(
                        "SELECT yes_ask, last_price FROM markets WHERE ticker=?", (ticker,)
                    )
                    if row:
                        stored = float(row.get("yes_ask") or row.get("last_price") or 0)
                        if stored > 0:
                            move_pct = abs(cur_ask - stored) / stored * 100
                            if move_pct >= 5.0:
                                direction = "yes" if cur_ask > stored else "no"
                                momentum_tickers[ticker] = {
                                    "move_pct": move_pct,
                                    "direction": direction,
                                    "stored": stored,
                                    "current": cur_ask,
                                }
                                logger.info(
                                    "  [MOMENTUM] %s: %.0f¢→%.0f¢ (%+.1f%%) → trade %s",
                                    ticker[:40], stored, cur_ask, cur_ask - stored, direction.upper()
                                )
            except Exception as e:
                logger.debug("Momentum check error: %s", e)

        # Pre-score — give live markets an extra boost for being time-sensitive
        prescored = []
        for market in live_markets:
            yes_ask = float(market.get("yes_ask") or market.get("last_price") or 0)
            title   = market.get("title", "")
            if yes_ask <= 1 or yes_ask >= 99:
                continue
            if not title or len(title) < 5 or title.startswith("0x"):
                continue
            pre = _pre_score(market) * 1.5   # live boost
            # Extra boost for price momentum markets
            ticker = market.get("ticker", "")
            if ticker in momentum_tickers:
                pre *= (1 + momentum_tickers[ticker]["move_pct"] / 50)
                market["_momentum"] = momentum_tickers[ticker]
            prescored.append((pre, market))

        prescored.sort(key=lambda x: x[0], reverse=True)
        logger.info(
            "── LIVE SCAN: %d markets pre-scored, sending top %d to AI ──",
            len(prescored), min(ai_eval_n, len(prescored)),
        )

        results = []
        for pre_score, market in prescored[:ai_eval_n]:
            ticker = market.get("ticker", "")
            if self._is_recently_rejected(ticker):
                logger.debug("Live AI skip (cached rejection): %s", ticker[:40])
                continue
            try:
                decision = await make_decision_for_market(
                    market, arb_signals, db=self.db, min_confidence=min_confidence
                )
            except Exception as e:
                logger.debug("Live AI eval failed for %s: %s", ticker, e)
                continue
            if not decision:
                continue

            conf   = float(decision.get("confidence", 0))
            net_ev = decision.get("net_ev") or 0.0
            action = decision.get("action", "HOLD")

            logger.info(
                "  [LIVE-AI] %-36s conf=%d%% ev=%.1f¢ → %s",
                (market.get("title") or ticker)[:36], conf, net_ev, action,
            )

            # Momentum override: if price moved strongly and AI is neutral, trade the momentum
            mom = market.get("_momentum")
            if mom and action != "BUY" and conf <= 55 and mom["move_pct"] >= 8.0:
                action   = "BUY"
                conf     = min(60.0, 50.0 + mom["move_pct"])
                net_ev   = mom["move_pct"] * 0.5   # estimated EV from momentum
                decision["action"]     = action
                decision["side"]       = mom["direction"]
                decision["confidence"] = conf
                decision["net_ev"]     = net_ev
                decision["reasoning"]  = (
                    f"[MOMENTUM] Price moved {mom['move_pct']:.1f}% "
                    f"({mom['stored']:.0f}¢→{mom['current']:.0f}¢) — "
                    f"trading {mom['direction'].upper()} with momentum. " + decision.get("reasoning", "")
                )[:500]
                logger.info(
                    "  [MOMENTUM-TRADE] %-36s move=%.1f%% → BUY %s conf=%.0f%%",
                    (market.get("title") or ticker)[:36], mom["move_pct"],
                    mom["direction"].upper(), conf,
                )

            if action != "BUY" or conf < min_confidence or net_ev <= 0:
                logger.info(
                    "  [LIVE-SKIP] %-36s action=%s conf=%d%% ev=%.1f¢ (need BUY+conf≥%d%%+ev>0)",
                    (market.get("title") or ticker)[:36], action, conf, net_ev, min_confidence,
                )
                self._mark_rejected(ticker, decision.get("reasoning", "")[:60])
                continue

            score     = score_opportunity(market, decision)
            yes_ask   = float(market.get("yes_ask") or market.get("last_price") or 0)
            no_ask    = float(market.get("no_ask")  or (100 - yes_ask))
            side      = decision.get("side", "yes")
            price_cents = yes_ask if side == "yes" else no_ask

            results.append({
                "market":      market,
                "decision":    decision,
                "poly_comp":   None,
                "score":       score,
                "side":        side,
                "price_cents": price_cents,
                "platform":    market.get("platform", "kalshi"),
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        picked = results[:top_n]

        if picked:
            logger.info(
                "LIVE TOP %d: %s",
                len(picked),
                " | ".join(
                    f"{r['market'].get('ticker','?')} {r['decision'].get('confidence',0):.0f}%"
                    for r in picked
                ),
            )
        return picked
