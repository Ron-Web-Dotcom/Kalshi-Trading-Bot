"""In-memory daily stats accumulator — resets at midnight UTC."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.daily_stats")


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
        self.poly_matches: int = 0
        self.suspicious_matches: List[Dict] = []
        self.consecutive_losses: int = 0

    # ── Recording methods ──────────────────────────────────────────────────

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
        self.poly_matches = 0
        self.suspicious_matches = []
        self.consecutive_losses = 0
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
            "top_opportunities": list(self.top_opportunities),
            "poly_matches":      self.poly_matches,
            "suspicious_matches": list(self.suspicious_matches),
        }


# Module-level singleton — import this everywhere
stats = DailyStats()
