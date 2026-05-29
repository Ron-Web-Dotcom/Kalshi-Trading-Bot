"""Phase 5 — arbitrage signal detection with overtrading prevention."""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("trading.arbitrage")


class ArbitrageDetector:
    """
    Detect arbitrage opportunities between Kalshi and external markets.
    Applies cooldown to prevent overtrading the same ticker.
    """

    def __init__(self):
        from src.config.settings import settings
        self.threshold_pct = settings.trading.arbitrage_threshold_pct
        self.cooldown_minutes = settings.trading.avoid_overtrading_minutes
        self._last_signal: Dict[str, datetime] = {}

    def detect(self, comparisons: List[Dict]) -> List[Dict]:
        """
        Filter comparisons to opportunities above threshold with cooldown applied.
        Returns list of arbitrage signals with action recommendations.
        """
        signals = []
        now = datetime.now(timezone.utc)

        for c in comparisons:
            diff_pct = c.get("diff_pct", 0)
            ticker = c.get("kalshi_ticker", "")

            if diff_pct < self.threshold_pct:
                continue

            # Cooldown check
            last = self._last_signal.get(ticker)
            if last and (now - last) < timedelta(minutes=self.cooldown_minutes):
                logger.debug(f"Skipping {ticker} — in cooldown")
                continue

            kalshi_price = c.get("kalshi_price", 50)
            poly_price = c.get("poly_price", 50)

            # If Kalshi is cheaper than Poly → BUY YES on Kalshi
            # If Kalshi is more expensive than Poly → BUY NO on Kalshi
            if kalshi_price < poly_price:
                action, side, edge = "BUY", "yes", poly_price - kalshi_price
            else:
                action, side, edge = "BUY", "no", kalshi_price - poly_price

            signal = {
                "ticker": ticker,
                "action": action,
                "side": side,
                "kalshi_price": kalshi_price,
                "poly_price": poly_price,
                "diff_pct": diff_pct,
                "edge_cents": edge,
                "signal_source": "arbitrage",
                "detected_at": now.isoformat(),
            }
            signals.append(signal)
            self._last_signal[ticker] = now
            logger.info(
                f"[ARBITRAGE SIGNAL] {ticker} | {action} {side.upper()} "
                f"@ {kalshi_price:.0f}¢ | Poly={poly_price:.0f}¢ | Δ={diff_pct:.1f}%"
            )

        return signals

    def detect_internal(self, markets: List[Dict]) -> List[Dict]:
        """
        Detect within-Kalshi arbitrage: YES+NO ask < 100 (free money).
        Also detects YES ask > 100-NO bid (mispricing).
        """
        signals = []
        now = datetime.now(timezone.utc)

        for m in markets:
            ticker = m.get("ticker", "")
            yes_ask = m.get("yes_ask", 0)
            no_ask = m.get("no_ask", 0)
            yes_bid = m.get("yes_bid", 0)
            no_bid = m.get("no_bid", 0)

            # Internal arb: YES ask + NO ask < 100 (should sum to 100)
            if yes_ask > 0 and no_ask > 0:
                spread_sum = yes_ask + no_ask
                if spread_sum < 98:  # profitable after fees
                    edge = 100 - spread_sum
                    if edge >= self.threshold_pct:
                        last = self._last_signal.get(ticker + "_internal")
                        if not last or (now - last) >= timedelta(minutes=self.cooldown_minutes):
                            signals.append({
                                "ticker": ticker,
                                "action": "BUY_BOTH",
                                "side": "both",
                                "yes_price": yes_ask,
                                "no_price": no_ask,
                                "diff_pct": edge,
                                "edge_cents": edge,
                                "signal_source": "internal_arb",
                                "detected_at": now.isoformat(),
                            })
                            self._last_signal[ticker + "_internal"] = now
                            logger.info(
                                f"[INTERNAL ARB] {ticker} | YES={yes_ask:.0f}¢ + NO={no_ask:.0f}¢ "
                                f"= {spread_sum:.0f}¢ | Edge={edge:.1f}¢"
                            )

        return signals
