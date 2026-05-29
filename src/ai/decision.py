"""Phase 10 — AI decision layer using Anthropic Claude."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.ai_decision")


@dataclass
class AIDecision:
    action: str          # BUY / SELL / HOLD
    confidence: float    # 0–100
    reasoning: str
    model: str
    ticker: str


class AIDecisionEngine:
    """
    Analyzes market data + arbitrage signals and decides BUY/SELL/HOLD.
    Only recommends a trade when confidence >= threshold.
    """

    def __init__(self, db=None):
        from src.config.settings import settings
        self.cfg = settings.ai
        self.trading_cfg = settings.trading
        self.db = db
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key)
        return self._client

    def _build_prompt(self, market: Dict, signals: List[Dict]) -> str:
        ticker = market.get("ticker", "")
        title = market.get("title", "")
        yes_ask = market.get("yes_ask", 0)
        no_ask = market.get("no_ask", 0)
        volume = market.get("volume", 0)
        open_interest = market.get("open_interest", 0)
        close_time = market.get("close_time", "unknown")

        signal_text = ""
        for s in signals:
            if s.get("ticker") == ticker:
                signal_text = (
                    f"\nArbitrage signal detected: {s.get('signal_source')} | "
                    f"diff={s.get('diff_pct', 0):.1f}% | edge={s.get('edge_cents', 0):.1f}¢"
                )

        implied_yes = yes_ask
        implied_no = no_ask
        spread = yes_ask + no_ask - 100
        liquidity_note = "LOW LIQUIDITY" if volume < 100 else ("HIGH LIQUIDITY" if volume > 10000 else "MEDIUM LIQUIDITY")

        return f"""You are a quantitative prediction market analyst. Analyze this Kalshi market and decide whether to trade.

Market: {ticker}
Question: {title}
YES ask: {yes_ask:.0f}¢  (implied YES probability: {implied_yes:.0f}%)
NO ask:  {no_ask:.0f}¢  (implied NO probability: {implied_no:.0f}%)
Market spread cost: {spread:.0f}¢ (YES+NO ask = {yes_ask+no_ask:.0f}¢; you pay this vig to trade both sides)
Volume: {volume:,}  |  Open interest: {open_interest:,}  |  Liquidity: {liquidity_note}
Closes: {close_time}
Kalshi taker fee: ~2% of notional (already factored into minimum edge requirement)
{signal_text}

Evaluate: Does a genuine edge exist AFTER accounting for the spread and 2% fee?

Respond with valid JSON only (no markdown):
{{
  "action": "BUY" | "SELL" | "HOLD",
  "side": "yes" | "no" | null,
  "confidence": <integer 0-100>,
  "reasoning": "<1-2 sentences explaining the decision>"
}}

Rules:
- BUY = enter a new position (on the stated side)
- Only recommend BUY if net edge after spread+fees is positive and material (>3¢ net expected value)
- Confidence must reflect your TRUE probability estimate vs market price, not just strength of signal
- Confidence > {self.trading_cfg.min_ai_confidence:.0f} required to generate a trade
- Low volume (<100) or extreme prices (<5¢ or >95¢) → HOLD unless arb signal is present
- When uncertain, choose HOLD — missing a trade is better than a bad one"""

    async def decide(self, market: Dict, signals: List[Dict] = []) -> AIDecision:
        ticker = market.get("ticker", "UNKNOWN")

        if not self.cfg.anthropic_api_key:
            logger.warning("No ANTHROPIC_API_KEY set — using rule-based fallback")
            return self._rule_based_decision(market, signals)

        prompt = self._build_prompt(market, signals)
        try:
            client = self._get_client()
            # Use sync client in async context via run_in_executor
            import asyncio
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(
                    model=self.cfg.model,
                    max_tokens=self.cfg.max_tokens,
                    temperature=self.cfg.temperature,
                    messages=[{"role": "user", "content": prompt}],
                )
            )
            raw = response.content[0].text.strip()
            data = json.loads(raw)
            action = data.get("action", "HOLD").upper()
            confidence = float(data.get("confidence", 0))
            reasoning = data.get("reasoning", "")

            decision = AIDecision(
                action=action,
                confidence=confidence,
                reasoning=reasoning,
                model=self.cfg.model,
                ticker=ticker,
            )

            logger.info(
                f"[AI] {ticker} → {action} | confidence={confidence:.0f}% | {reasoning[:80]}"
            )

            # Persist decision
            if self.db:
                now = datetime.now(timezone.utc).isoformat()
                try:
                    usage = response.usage
                    cost = (usage.input_tokens * 3e-6 + usage.output_tokens * 15e-6)
                    await self.db.insert("ai_decisions", {
                        "ticker": ticker,
                        "action": action,
                        "confidence": confidence,
                        "reasoning": reasoning,
                        "model": self.cfg.model,
                        "prompt_tokens": usage.input_tokens,
                        "completion_tokens": usage.output_tokens,
                        "cost_usd": cost,
                        "decided_at": now,
                    })
                except Exception:
                    pass

            return decision

        except json.JSONDecodeError as e:
            logger.warning(f"AI response parse error for {ticker}: {e}")
            return self._rule_based_decision(market, signals)
        except Exception as e:
            logger.error(f"AI decision error for {ticker}: {e}")
            return self._rule_based_decision(market, signals)

    def _rule_based_decision(self, market: Dict, signals: List[Dict]) -> AIDecision:
        """Fallback: rule-based decision when AI is unavailable."""
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask", 50)
        volume = market.get("volume", 0)

        # Check for arbitrage signal — scale confidence with edge size
        for s in signals:
            diff = s.get("diff_pct", 0)
            if s.get("ticker") == ticker and diff >= 5:
                # 5% diff → 72%, 10% diff → 80%, 20%+ diff → 90% cap
                confidence = min(70.0 + diff, 90.0)
                return AIDecision(
                    action="BUY",
                    confidence=confidence,
                    reasoning=f"Arbitrage signal: {diff:.1f}% price difference detected",
                    model="rule_based",
                    ticker=ticker,
                )

        # Low volume = low confidence
        if volume < 100:
            return AIDecision(action="HOLD", confidence=30.0,
                              reasoning="Insufficient volume", model="rule_based", ticker=ticker)

        # Extreme mispricing
        if yes_ask < 5 or yes_ask > 95:
            return AIDecision(action="HOLD", confidence=40.0,
                              reasoning="Extreme price — resolution likely imminent",
                              model="rule_based", ticker=ticker)

        return AIDecision(action="HOLD", confidence=50.0,
                          reasoning="No clear edge detected", model="rule_based", ticker=ticker)

    def should_trade(self, decision: AIDecision) -> bool:
        return (
            decision.action in ("BUY", "SELL")
            and decision.confidence >= self.trading_cfg.min_ai_confidence
        )
