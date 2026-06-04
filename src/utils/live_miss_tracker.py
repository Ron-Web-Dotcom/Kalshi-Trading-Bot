"""
Live Miss Tracker — tracks predictions the bot evaluated during live events
but couldn't place (hit daily limit, low confidence, no EV, etc.).

When the event resolves, compares the bot's predicted direction against the
actual outcome. If the bot was RIGHT but didn't bet, that's a confirmed miss.

Two scan types:
  LIVE SCAN   — events happening RIGHT NOW (every 5 min)
                Sports in progress, crypto moving, election counting, etc.
                These are the most painful misses — money left on the table TODAY

  REGULAR SCAN — markets not currently live (existing trade.py flow)
                 Tracked separately, shown in the standard near_miss_digest

Storage:
  In-memory dict (not DB) — misses reset each day at midnight.
  Each entry: ticker, title, side, confidence, yes_ask, predicted_outcome,
              scan_type, skip_reason, reasoning, scanned_at, resolved_at,
              actual_outcome, was_correct, potential_pnl

The hourly digest:
  - Shows only LIVE-scan misses that RESOLVED IN THE LAST HOUR
  - Never repeats: tracks sent tickers per window, rotates each hour
  - Highlights "would have earned $X" using the price at scan time
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("trading.live_miss_tracker")

# ── In-memory store ───────────────────────────────────────────────────────────

class LiveMissTracker:
    """
    Singleton tracker for live-scan misses.
    Call record_live_miss() when the bot evaluates a live market but skips.
    Call check_resolutions() periodically to detect correct predictions.
    Call hourly_digest() to get new confirmed misses for Discord.
    """

    def __init__(self):
        # ticker → miss entry
        self._misses: Dict[str, Dict] = {}
        # tickers included in the LAST hourly digest — don't repeat them
        self._last_digest_tickers: set = set()
        self._last_digest_at: Optional[datetime] = None
        self._day = datetime.now(timezone.utc).date()

    def _maybe_reset(self):
        today = datetime.now(timezone.utc).date()
        if today != self._day:
            self._misses.clear()
            self._last_digest_tickers.clear()
            self._last_digest_at = None
            self._day = today

    def record(
        self,
        ticker: str,
        title: str,
        side: str,            # bot's predicted side (yes/no)
        confidence: float,
        yes_ask: float,
        no_ask: float,
        reasoning: str,
        skip_reason: str,
        scan_type: str,       # "live" or "regular"
        net_ev: Optional[float] = None,
        true_prob: Optional[float] = None,
        platform: str = "kalshi",
    ) -> None:
        """Record a skipped prediction during a live scan."""
        self._maybe_reset()
        # Update existing entry (keep freshest reasoning)
        self._misses[ticker] = {
            "ticker":          ticker,
            "title":           title,
            "side":            side,
            "confidence":      confidence,
            "yes_ask":         yes_ask,
            "no_ask":          no_ask,
            "net_ev":          net_ev,
            "true_prob":       true_prob,
            "reasoning":       reasoning,
            "skip_reason":     skip_reason,
            "scan_type":       scan_type,
            "platform":        platform,
            "scanned_at":      datetime.now(timezone.utc).isoformat(),
            "resolved_at":     None,
            "actual_outcome":  None,   # "yes" or "no"
            "was_correct":     None,   # True / False
            "potential_pnl":   None,   # what we would have made per $10
        }
        logger.debug("LiveMiss recorded: %s | %s | conf=%.0f%% | skip=%s",
                     ticker[:40], scan_type, confidence, skip_reason[:40])

    def mark_resolved(self, ticker: str, actual_outcome: str) -> bool:
        """
        Mark a tracked miss as resolved with the real outcome.
        Returns True if the bot's prediction was correct.
        """
        self._maybe_reset()
        entry = self._misses.get(ticker)
        if not entry or entry.get("resolved_at"):
            return False

        predicted = (entry.get("side") or "yes").lower()
        actual    = actual_outcome.lower()
        correct   = (predicted == actual)

        # Calculate potential PnL on a $10 stake
        if correct:
            price = entry["yes_ask"] if predicted == "yes" else entry["no_ask"]
            contracts = 10.0 / (price / 100) if price > 0 else 0
            pnl_per_10 = contracts * (100 - price) * 0.98 / 100
        else:
            pnl_per_10 = -(entry["yes_ask"] if predicted == "yes" else entry["no_ask"]) / 10

        entry["resolved_at"]    = datetime.now(timezone.utc).isoformat()
        entry["actual_outcome"] = actual
        entry["was_correct"]    = correct
        entry["potential_pnl"]  = round(pnl_per_10, 2)

        logger.info(
            "LiveMiss resolved: %s | predicted=%s actual=%s | correct=%s | pot_pnl=%.2f",
            ticker[:40], predicted, actual, correct, pnl_per_10,
        )
        return correct

    def new_confirmed_misses(self, window_hours: float = 1.0, scan_type: str = "live") -> List[Dict]:
        """
        Return misses that:
          - Were correct predictions
          - Resolved within the last `window_hours`
          - NOT already included in the last digest
          - Match the given scan_type
        Sorted by potential_pnl descending (most painful miss first).
        """
        self._maybe_reset()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        result = []
        for ticker, entry in self._misses.items():
            if not entry.get("was_correct"):
                continue
            if entry.get("scan_type") != scan_type:
                continue
            if ticker in self._last_digest_tickers:
                continue
            resolved_at_str = entry.get("resolved_at")
            if not resolved_at_str:
                continue
            try:
                resolved_at = datetime.fromisoformat(resolved_at_str)
                if resolved_at.tzinfo is None:
                    resolved_at = resolved_at.replace(tzinfo=timezone.utc)
                if resolved_at < cutoff:
                    continue
            except Exception:
                continue
            result.append(entry)

        result.sort(key=lambda e: e.get("potential_pnl") or 0, reverse=True)
        return result

    def mark_digest_sent(self, tickers: List[str]) -> None:
        """Call after sending a digest to prevent those tickers repeating next hour."""
        self._last_digest_tickers = set(tickers)
        self._last_digest_at = datetime.now(timezone.utc)

    def all_live_misses(self) -> List[Dict]:
        """All recorded live-scan misses today (resolved or pending)."""
        self._maybe_reset()
        return [e for e in self._misses.values() if e.get("scan_type") == "live"]

    def pending_resolution(self) -> List[Dict]:
        """Live misses not yet resolved — still watching these."""
        self._maybe_reset()
        return [
            e for e in self._misses.values()
            if e.get("scan_type") == "live" and not e.get("resolved_at")
        ]


# Module-level singleton
live_miss_tracker = LiveMissTracker()
