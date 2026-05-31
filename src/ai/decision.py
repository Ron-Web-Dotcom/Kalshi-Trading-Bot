"""Phase 10 — AI decision layer using Anthropic Claude."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.ai_decision")


@dataclass
class AIDecision:
    action: str          # BUY / HOLD
    confidence: float    # 0–100
    reasoning: str
    model: str
    ticker: str
    side: str = "yes"    # yes / no — which side to buy
    true_prob: Optional[float] = None   # AI's estimated P(YES)
    net_ev: Optional[float]   = None    # expected value per contract in cents


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

    def _build_prompt(self, market: Dict, signals: List[Dict], context: str = "") -> str:
        ticker        = market.get("ticker", "")
        title         = market.get("title", "")
        yes_ask       = market.get("yes_ask", 0)
        no_ask        = market.get("no_ask", 0)
        volume        = market.get("volume", 0)
        open_interest = market.get("open_interest", 0)
        close_time    = market.get("close_time", "unknown")

        arb_text = ""
        for s in signals:
            if s.get("ticker") == ticker:
                arb_text = (
                    f"\nArbitrage signal: {s.get('signal_source')} | "
                    f"diff={s.get('diff_pct', 0):.1f}% | net edge={s.get('edge_cents', 0):.1f}¢"
                )

        # Polymarket cross-reference (injected by opportunity hunter when available)
        poly_yes   = market.get("poly_yes")
        poly_no    = market.get("poly_no")
        poly_block = ""
        if poly_yes is not None and poly_no is not None:
            poly_diff_yes = poly_yes - yes_ask
            poly_diff_no  = poly_no  - no_ask
            poly_block = (
                f"\n=== POLYMARKET CROSS-REFERENCE ===\n"
                f"Polymarket YES: {poly_yes:.0f}¢  |  Polymarket NO: {poly_no:.0f}¢\n"
                f"Gap vs Kalshi: YES {poly_diff_yes:+.0f}¢  |  NO {poly_diff_no:+.0f}¢\n"
                f"(Polymarket is an independent prediction market — significant gaps suggest mispricing on one platform)"
            )

        # Pre-compute EV helpers so Claude reasons correctly
        yes_ev_if_true = (100 - yes_ask) * 0.98
        no_ev_if_true  = (100 - no_ask)  * 0.98
        spread         = yes_ask + no_ask - 100
        liquidity      = "LOW" if volume < 100 else "HIGH" if volume > 10000 else "MEDIUM"
        context_block  = f"\n\n--- REAL-WORLD CONTEXT ---\n{context}\n--- END CONTEXT ---" if context else ""

        return f"""You are an expert quantitative prediction market trader. Your ONLY goal is to find bets where your estimated true probability beats the market price by enough to profit after fees.

=== MARKET ===
Ticker:   {ticker}
Question: {title}
Closes:   {close_time}

=== KALSHI PRICES (cents — market pays 100¢ on correct resolution) ===
YES ask: {yes_ask:.0f}¢  → Kalshi implies {yes_ask:.0f}% YES probability
NO ask:  {no_ask:.0f}¢  → Kalshi implies {no_ask:.0f}% NO probability
Spread:  {spread:.0f}¢  |  Volume: {volume:,}  |  Liquidity: {liquidity}
{poly_block}
=== EXPECTED VALUE on Kalshi (per contract, after 2% fee) ===
BUY YES @ {yes_ask:.0f}¢ → win {yes_ev_if_true:.1f}¢ if YES  |  lose {yes_ask:.0f}¢ if NO  |  break-even P(YES) = {yes_ask/98*100:.1f}%
BUY NO  @ {no_ask:.0f}¢ → win {no_ev_if_true:.1f}¢ if NO   |  lose {no_ask:.0f}¢ if YES |  break-even P(NO)  = {no_ask/98*100:.1f}%
{arb_text}{context_block}

=== YOUR TASK ===
Step 1 — Use the real-world context AND the Polymarket cross-reference (if present) to estimate TRUE P(YES).
         Polymarket having a significantly different price is a strong signal one platform is wrong.
Step 2 — Compute net EV = (your_true_prob/100 - kalshi_price/100) × 98¢  for the better side.
Step 3 — BUY only if: |net_ev| > 4¢  AND  volume ≥ 100  AND  price between 5–95¢  AND  context supports it.
Step 4 — HOLD if context is thin or the edge is marginal. Cash is a valid position.

Respond ONLY with this exact JSON (no markdown):
{{
  "true_prob_yes": <your estimated 0-100 probability that YES resolves>,
  "action": "BUY" | "HOLD",
  "side": "yes" | "no" | null,
  "net_ev_cents": <expected profit per contract after fee, negative = unfavourable>,
  "confidence": <integer 0-100 — certainty in your true_prob estimate>,
  "reasoning": "<2-3 sentences citing specific facts that drove your estimate>"
}}

Rules:
- true_prob must be driven by FACTS from context, not gut feel. No context = be very conservative.
- Polymarket price divergence is a useful signal but not sufficient alone — check the real-world data.
- confidence = certainty in your probability, not excitement about the trade
- confidence ≥ {self.trading_cfg.min_ai_confidence:.0f} required to place a trade
- HOLD is almost always safer than a weakly-supported BUY"""

    async def decide(self, market: Dict, signals: List[Dict] = []) -> AIDecision:
        ticker = market.get("ticker", "UNKNOWN")

        if not self.cfg.anthropic_api_key:
            logger.warning("No ANTHROPIC_API_KEY set — using rule-based fallback")
            return self._rule_based_decision(market, signals)

        try:
            from src.data.context_builder import build_market_context
            context = await build_market_context(market)
        except Exception:
            context = ""

        prompt = self._build_prompt(market, signals, context)
        try:
            client = self._get_client()
            # Use sync client in async context via run_in_executor
            import asyncio
            loop = asyncio.get_running_loop()
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
            # Strip markdown fences if model wraps JSON
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())

            action      = data.get("action", "HOLD").upper()
            side        = (data.get("side") or "yes").lower()
            confidence  = float(data.get("confidence", 0))
            reasoning   = data.get("reasoning", "")
            true_prob   = data.get("true_prob_yes")
            net_ev      = data.get("net_ev_cents")

            # Extra guard: reject low EV even if model says BUY
            if action == "BUY" and net_ev is not None and float(net_ev) < 4.0:
                action = "HOLD"
                reasoning = f"[EV guard: net_ev={net_ev:.1f}¢ < 4¢ threshold] " + reasoning

            decision = AIDecision(
                action=action,
                confidence=confidence,
                reasoning=reasoning,
                model=self.cfg.model,
                ticker=ticker,
                side=side,
                true_prob=true_prob,
                net_ev=net_ev,
            )

            ev_str = f" | EV={net_ev:.1f}¢" if net_ev is not None else ""
            tp_str = f" | P(YES)={true_prob:.0f}%" if true_prob is not None else ""
            logger.info(
                "[AI] %s → %s/%s | conf=%d%%%s%s | %s",
                ticker, action, side.upper(), confidence, tp_str, ev_str, reasoning[:80],
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

    async def evaluate_open_position(
        self,
        position: Dict,
        market: Dict,
        context: str = "",
    ) -> Dict:
        """
        Re-evaluate an open position against fresh real-world data.

        Returns dict:
          {"verdict": "HOLD" | "EXIT", "confidence": float, "reasoning": str}

        EXIT means the AI believes the original thesis has broken down and
        we should close now rather than wait for stop-loss / take-profit.
        """
        ticker     = position.get("ticker", "")
        side       = position.get("side", "yes")
        avg_price  = float(position.get("avg_price", 0))
        contracts  = int(position.get("contracts", 0))
        cur_price  = float(position.get("current_price") or avg_price)
        unrealised = (cur_price - avg_price) * contracts / 100
        pct_change = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0
        title      = market.get("title", "")
        yes_ask    = market.get("yes_ask", 0)
        no_ask     = market.get("no_ask", 0)
        close_time = market.get("close_time", "unknown")

        context_block = f"\n\n--- FRESH REAL-WORLD CONTEXT ---\n{context}\n--- END CONTEXT ---" if context else ""

        prompt = f"""You are reviewing an OPEN prediction market position. Based on the latest real-world data, decide whether to HOLD or EXIT early.

=== OPEN POSITION ===
Market:    {ticker}
Question:  {title}
Our side:  {side.upper()}  (we profit if {side.upper()} resolves)
Entry:     {avg_price:.0f}¢ per contract
Current:   {cur_price:.0f}¢ (on the {side} side)
Contracts: {contracts}
Unrealised PnL: ${unrealised:+.2f}  ({pct_change:+.1f}% from entry)
Closes:    {close_time}

=== LIVE MARKET ===
YES ask: {yes_ask:.0f}¢  |  NO ask: {no_ask:.0f}¢
{context_block}

=== YOUR TASK ===
The original bet was that {side.upper()} would resolve. Has that thesis changed?

Answer HOLD if:
- The real-world evidence still supports our {side.upper()} position
- Or there is no strong new evidence either way

Answer EXIT if:
- New facts directly contradict the {side.upper()} thesis with high confidence
- The situation has fundamentally changed since entry
- Cutting now (even at a small loss) is clearly better than holding to resolution

Respond ONLY with this JSON (no markdown):
{{
  "verdict": "HOLD" | "EXIT",
  "confidence": <integer 0-100 — how certain you are in this verdict>,
  "reasoning": "<2-3 sentences explaining what changed or why you're holding>"
}}

Important: bias toward HOLD — only EXIT when evidence is clear and strong (confidence >= 75)."""

        if not self.cfg.anthropic_api_key:
            return {"verdict": "HOLD", "confidence": 50, "reasoning": "No AI key — holding by default"}

        try:
            client = self._get_client()
            import asyncio
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(
                    model=self.cfg.model,
                    max_tokens=512,
                    temperature=0.2,
                    messages=[{"role": "user", "content": prompt}],
                )
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data       = json.loads(raw.strip())
            verdict    = data.get("verdict", "HOLD").upper()
            confidence = float(data.get("confidence", 50))
            reasoning  = data.get("reasoning", "")
            logger.info(
                "[REEVAL] %s %s → %s | conf=%d%% | %s",
                ticker, side.upper(), verdict, confidence, reasoning[:80],
            )
            return {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
        except Exception as e:
            logger.debug("Re-eval error for %s: %s", ticker, e)
            return {"verdict": "HOLD", "confidence": 0, "reasoning": f"Re-eval failed: {e}"}
