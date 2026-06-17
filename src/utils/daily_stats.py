"""In-memory daily stats accumulator — resets at midnight UTC."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.daily_stats")


def _build_eval_entry(
    ticker: str,
    action: str,
    side: str,
    confidence: float,
    net_ev,
    true_prob,
    reasoning: str,
    title: str = "",
    platform: str = "kalshi",
    close_time: str = "",
    yes_ask: float = 0.0,
    evaluated_at: str = "",
) -> dict:
    """Single source of truth for evaluation entry shape. All fields always present."""
    from datetime import datetime, timezone
    return {
        "ticker":      ticker,
        "title":       title,
        "action":      action,
        "side":        side,
        "confidence":  float(confidence or 0),
        "net_ev":      net_ev,
        "true_prob":   true_prob,
        "reasoning":   reasoning or "",
        "platform":    platform or "kalshi",
        "close_time":  close_time or "",
        "yes_ask":     float(yes_ask or 0),
        "price_cents": float(yes_ask or 0),   # alias used by bot_alert_loop
        "evaluated_at": evaluated_at or datetime.now(timezone.utc).isoformat(),
    }


class DailyStats:
    """
    Singleton in-memory tracker for daily trading metrics.

    All counters reset at midnight UTC via reset_for_new_day().
    bot_start_time is set once and never reset.
    """

    def __init__(self) -> None:
        self.bot_start_time: Optional[datetime] = datetime.now(timezone.utc)
        self.markets_scanned: int = 0
        self.signals_generated: int = 0
        self.trades_executed: int = 0
        self.trades_skipped: int = 0
        self.errors: List[Tuple[datetime, str]] = []
        self.top_opportunities: List[Dict] = []
        self.all_evaluations: List[Dict] = []   # every AI evaluation including HOLDs
        self.near_misses: List[Dict] = []        # BUY signals that fell just short
        self.poly_matches: int = 0
        self.suspicious_matches: List[Dict] = []
        self.consecutive_losses: int = 0
        # Live scan state — updated by live_miss_scan_loop each cycle
        self.last_live_scan_markets: List[Dict] = []   # confirmed live-now markets this hour
        self.last_regular_scan_top: List[Dict] = []    # top pre-scored regular candidates this hour
        self.last_scan_updated_at: str = ""

    def update_scan_state(
        self,
        live_markets: List[Dict],
        regular_top: List[Dict],
    ) -> None:
        """Called after each scan cycle to keep heartbeat data fresh."""
        self.last_live_scan_markets = live_markets[:6]
        self.last_regular_scan_top  = regular_top[:6]
        self.last_scan_updated_at   = datetime.now(timezone.utc).isoformat()

    # ── Recording methods ──────────────────────────────────────────────────

    def record_evaluation(
        self,
        ticker: str,
        action: str,
        side: str,
        confidence: float,
        net_ev: Optional[float],
        true_prob: Optional[float],
        reasoning: str,
        title: str = "",
        platform: str = "kalshi",
        close_time: str = "",
        yes_ask: float = 0.0,
    ) -> None:
        """Record every AI evaluation — BUY or HOLD — to find best pick of the day."""
        entry = _build_eval_entry(
            ticker=ticker, action=action, side=side, confidence=confidence,
            net_ev=net_ev, true_prob=true_prob, reasoning=reasoning,
            title=title, platform=platform, close_time=close_time, yes_ask=yes_ask,
        )
        self.all_evaluations.append(entry)
        # Keep top 20 by confidence
        self.all_evaluations.sort(key=lambda x: x["confidence"], reverse=True)
        self.all_evaluations = self.all_evaluations[:20]

    def best_pick(self) -> Optional[Dict]:
        """Return the highest-confidence evaluation of the day (any platform)."""
        return self.all_evaluations[0] if self.all_evaluations else None

    def best_pick_by_platform(self) -> Dict[str, Optional[Dict]]:
        """Return the best pick per platform — Kalshi and Polymarket each get a fair slot."""
        kal  = next((e for e in self.all_evaluations if e.get("platform", "kalshi") != "polymarket"), None)
        poly = next((e for e in self.all_evaluations if e.get("platform") == "polymarket"), None)
        return {"kalshi": kal, "polymarket": poly}

    def record_near_miss(
        self,
        ticker: str,
        title: str,
        side: str,
        confidence: float,
        net_ev: Optional[float],
        true_prob: Optional[float],
        reasoning: str,
        platform: str = "kalshi",
        skip_reason: str = "",
        recorded_at: str = "",
    ) -> None:
        """Record a BUY signal that didn't clear the confidence bar. Deduplicates by ticker."""
        # Remove any existing entry for this ticker (keep freshest)
        self.near_misses = [n for n in self.near_misses if n["ticker"] != ticker]
        self.near_misses.append({
            "ticker":      ticker,
            "title":       title or ticker[:32],
            "side":        side,
            "confidence":  confidence,
            "net_ev":      net_ev,
            "true_prob":   true_prob,
            "reasoning":   reasoning,
            "platform":    platform,
            "skip_reason": skip_reason or "confidence below threshold",
            "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
        })
        # Keep top 5 by confidence (highest-confidence misses are most interesting)
        self.near_misses.sort(key=lambda x: x.get("confidence") or 0, reverse=True)
        self.near_misses = self.near_misses[:5]

    def top_near_misses(self, n: int = 5) -> List[Dict]:
        """Return up to n best near-misses of the day, newest-first within same confidence."""
        return self.near_misses[:n]

    def record_signal(self, ticker: str, confidence: float, net_ev: Optional[float], action: str) -> None:
        """Increment signals_generated if action is BUY."""
        if action and action.upper() == "BUY":
            self.signals_generated += 1
            logger.debug("Signal recorded: %s conf=%.0f%% EV=%s", ticker, confidence, net_ev)

    def record_trade(
        self,
        ticker: str,
        side: str,
        confidence: float,
        net_ev: Optional[float],
        score: float,
        reasoning: str,
    ) -> None:
        """Increment trades_executed and maintain top 5 opportunities by score."""
        self.trades_executed += 1
        entry = {
            "ticker":     ticker,
            "score":      score,
            "confidence": confidence,
            "net_ev":     net_ev,
            "side":       side,
            "reasoning":  reasoning,
        }
        self.top_opportunities.append(entry)
        # Keep only top 5 by score
        self.top_opportunities.sort(key=lambda x: x["score"], reverse=True)
        self.top_opportunities = self.top_opportunities[:5]

    def record_skip(self, reason: str) -> None:
        """Increment trades_skipped."""
        self.trades_skipped += 1
        logger.debug("Trade skipped: %s", reason)

    def record_error(self, msg: str) -> None:
        """Append error to the list (max 50 retained)."""
        now = datetime.now(timezone.utc)
        self.errors.append((now, msg))
        if len(self.errors) > 50:
            self.errors = self.errors[-50:]

    def record_markets_scanned(self, n: int) -> None:
        """Add n to the running markets_scanned total."""
        self.markets_scanned += n

    def record_loss(self) -> None:
        """Increment consecutive_losses counter."""
        self.consecutive_losses += 1
        logger.debug("Consecutive losses: %d", self.consecutive_losses)

    def record_win(self) -> None:
        """Reset consecutive_losses to 0."""
        self.consecutive_losses = 0

    def record_poly_match(
        self,
        ticker: str,
        jaccard: float,
        net_edge: float,
        suspicious: bool = False,
    ) -> None:
        """Increment poly_matches; append suspicious entries."""
        self.poly_matches += 1
        if suspicious:
            self.suspicious_matches.append({
                "ticker":   ticker,
                "jaccard":  jaccard,
                "net_edge": net_edge,
            })

    # ── Utility methods ────────────────────────────────────────────────────

    def uptime_str(self) -> str:
        """Return human-readable uptime, e.g. '2d 4h 32m'."""
        if not self.bot_start_time:
            return "unknown"
        delta = datetime.now(timezone.utc) - self.bot_start_time
        total_seconds = int(delta.total_seconds())
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    def reset_for_new_day(self) -> None:
        """Reset all daily counters. bot_start_time is preserved."""
        self.markets_scanned = 0
        self.signals_generated = 0
        self.trades_executed = 0
        self.trades_skipped = 0
        self.errors = []
        self.top_opportunities = []
        self.near_misses = []
        self.poly_matches = 0
        self.suspicious_matches = []
        self.consecutive_losses = 0
        self.all_evaluations = []   # reset daily — fresh picks each day
        self.last_live_scan_markets = []
        self.last_regular_scan_top  = []
        self.last_scan_updated_at   = ""
        logger.info("Daily stats reset for new day.")

    def snapshot(self) -> Dict:
        """Return a dict of all current stats."""
        return {
            "bot_start_time":    self.bot_start_time.isoformat() if self.bot_start_time else None,
            "uptime":            self.uptime_str(),
            "markets_scanned":   self.markets_scanned,
            "signals_generated": self.signals_generated,
            "trades_executed":   self.trades_executed,
            "trades_skipped":    self.trades_skipped,
            "errors":            [(ts.isoformat(), msg) for ts, msg in self.errors],
            "top_opportunities":  list(self.top_opportunities),
            "poly_matches":       self.poly_matches,
            "suspicious_matches": list(self.suspicious_matches),
            "consecutive_losses": self.consecutive_losses,
        }


# Module-level singleton — import this everywhere
stats = DailyStats()
