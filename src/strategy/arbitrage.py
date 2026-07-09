"""Phase 5 — arbitrage signal detection with overtrading prevention."""

import logging
from datetime import datetime, timedelta
from typing import Dict, List
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.arbitrage")

# Kalshi taker fee per leg (~2% of notional)
KALSHI_FEE_PCT = 0.02


class ArbitrageDetector:
    """
    Detect arbitrage opportunities:
      1. Cross-market: Kalshi price vs Polymarket price
      2. Internal: YES ask + NO ask < 100¢ (risk-free both legs)

    Edge is always computed AFTER Kalshi fees so we only flag genuinely
    profitable opportunities.
    """

    def __init__(self):
        from src.config.settings import settings
        self.threshold_pct = settings.trading.arbitrage_threshold_pct
        self.cooldown_minutes = settings.trading.avoid_overtrading_minutes
        self._last_signal: Dict[str, datetime] = {}

    # ── Cross-market arbitrage ────────────────────────────────────────────────

    def detect(self, comparisons: List[Dict]) -> List[Dict]:
        """
        Filter price comparisons (Kalshi vs Polymarket) to real arbitrage
        opportunities.  Net edge = price_diff - 2×fee_on_kalshi_leg.
        Returns signals with action/side pre-determined.
        """
        signals = []
        now = datetime.now(_ET)

        for c in comparisons:
            diff_pct = c.get("diff_pct", 0)
            ticker = c.get("kalshi_ticker", "")

            if diff_pct < self.threshold_pct:
                continue

            # Cooldown
            last = self._last_signal.get(ticker)
            if last and (now - last) < timedelta(minutes=self.cooldown_minutes):
                logger.debug(f"Skipping {ticker} — in arb cooldown")
                continue

            kalshi_price = c.get("kalshi_price", 50)
            poly_price = c.get("poly_price", 50)

            if kalshi_price < poly_price:
                # Buy YES on Kalshi (it's cheaper); Poly implies higher probability
                side = "yes"
                gross_edge = poly_price - kalshi_price
            else:
                # Buy NO on Kalshi (it's cheaper); Poly implies lower probability
                side = "no"
                gross_edge = kalshi_price - poly_price

            # Deduct Kalshi fee from gross edge
            fee_cents = (kalshi_price if side == "yes" else (100 - kalshi_price)) * KALSHI_FEE_PCT
            net_edge = gross_edge - fee_cents

            if net_edge <= 0:
                logger.debug(f"[ARB] {ticker} gross={gross_edge:.1f}¢ fee={fee_cents:.1f}¢ → no net edge")
                continue

            signal = {
                "ticker": ticker,
                "action": "BUY",
                "side": side,
                "kalshi_price": kalshi_price,
                "poly_price": poly_price,
                "diff_pct": diff_pct,
                "gross_edge_cents": gross_edge,
                "edge_cents": net_edge,
                "fee_cents": fee_cents,
                "signal_source": "cross_market_arb",
                "detected_at": now.isoformat(),
            }
            signals.append(signal)
            self._last_signal[ticker] = now
            logger.info(
                f"[CROSS-MARKET ARB] {ticker} | BUY {side.upper()} @ {kalshi_price:.0f}¢ "
                f"| Poly={poly_price:.0f}¢ | Gross={gross_edge:.1f}¢ Fee={fee_cents:.1f}¢ "
                f"Net={net_edge:.1f}¢ | Δ={diff_pct:.1f}%"
            )

        return signals

    # ── Internal (within-Kalshi) arbitrage ───────────────────────────────────

    def detect_internal(self, markets: List[Dict]) -> List[Dict]:
        """
        Detect risk-free opportunities where YES ask + NO ask < 100¢.
        Gross profit = 100 - (yes_ask + no_ask).
        Net profit after two Kalshi fees = gross - yes_ask×fee - no_ask×fee.
        Only flag if net > 0 and above threshold.
        """
        signals = []
        now = datetime.now(_ET)

        for m in markets:
            ticker = m.get("ticker", "")
            yes_ask = m.get("yes_ask", 0)
            no_ask = m.get("no_ask", 0)

            if not (yes_ask > 0 and no_ask > 0):
                continue

            spread_sum = yes_ask + no_ask
            if spread_sum >= 100:
                continue

            gross_edge = 100 - spread_sum
            # Fee on each leg (we pay fee twice — once for YES, once for NO)
            total_fee = (yes_ask + no_ask) * KALSHI_FEE_PCT
            net_edge = gross_edge - total_fee

            # threshold_pct is in percent; net_edge is in cents — compare apples to apples
            # A net_edge of 4¢ on a ~100¢ payout = 4% ROI, so threshold_pct≈cents here
            # Use a minimum absolute edge of 2¢ to avoid fee-eroded trades
            threshold_cents = self.threshold_pct  # 1 pct-point == 1 cent on Kalshi's 0-100 cent scale
            if net_edge < max(threshold_cents, 2.0):
                continue

            key = ticker + "_internal"
            last = self._last_signal.get(key)
            if last and (now - last) < timedelta(minutes=self.cooldown_minutes):
                continue

            signals.append({
                "ticker": ticker,
                "action": "BUY_BOTH",
                "side": "both",
                "yes_price": yes_ask,
                "no_price": no_ask,
                "diff_pct": net_edge,
                "gross_edge_cents": gross_edge,
                "edge_cents": net_edge,
                "fee_cents": total_fee,
                "signal_source": "internal_arb",
                "detected_at": now.isoformat(),
            })
            self._last_signal[key] = now
            logger.info(
                f"[INTERNAL ARB] {ticker} | YES={yes_ask:.0f}¢ + NO={no_ask:.0f}¢ "
                f"= {spread_sum:.0f}¢ | Gross={gross_edge:.1f}¢ Fee={total_fee:.1f}¢ "
                f"Net={net_edge:.1f}¢ ← RISK FREE"
            )

        return signals
