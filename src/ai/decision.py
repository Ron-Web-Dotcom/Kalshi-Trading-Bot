"""Phase 10 — AI decision layer using OpenAI."""

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
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self.cfg.openai_api_key)
        return self._client

    async def _build_bot_context(self) -> str:
        """Build a self-awareness block — bot's own track record, positions, risk state."""
        try:
            from src.utils.daily_stats import stats as daily_stats
            from src.config.settings import settings

            snap    = daily_stats.snapshot()
            lines   = ["=== BOT SELF-AWARENESS ==="]

            # Win rate and track record
            total   = snap.get("trades_executed", 0)
            if self.db:
                try:
                    wl = await self.db.fetchone(
                        "SELECT COUNT(*) as total, "
                        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                        "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses, "
                        "COALESCE(SUM(pnl),0) as total_pnl "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL"
                    ) or {}
                    total_closed = wl.get("total", 0) or 0
                    wins         = wl.get("wins",  0) or 0
                    total_pnl    = wl.get("total_pnl", 0.0) or 0.0
                    win_rate     = (wins / total_closed * 100) if total_closed > 0 else 0.0
                    lines.append(
                        f"Track record: {total_closed} trades closed | "
                        f"Win rate: {win_rate:.0f}% | All-time PnL: ${total_pnl:+.2f}"
                    )
                    if total_closed == 0:
                        lines.append("Status: No completed trades yet — this is an early decision.")

                    # Recent last 5 closed trades
                    recent = await self.db.fetchall(
                        "SELECT ticker, side, pnl, close_reason, avg_price, current_price "
                        "FROM positions WHERE status='closed' AND pnl IS NOT NULL "
                        "ORDER BY closed_at DESC LIMIT 5"
                    ) or []
                    if recent:
                        lines.append("Last 5 closed trades:")
                        for r in recent:
                            outcome = "WIN" if (r.get("pnl") or 0) > 0 else "LOSS"
                            lines.append(
                                f"  {outcome} | {r.get('ticker','')} {(r.get('side') or '').upper()} "
                                f"| entry={r.get('avg_price',0):.0f}¢ exit={r.get('current_price',0):.0f}¢ "
                                f"| PnL=${r.get('pnl',0):+.2f} | reason={r.get('close_reason','')}"
                            )

                    # Current open positions
                    open_pos = await self.db.fetchall(
                        "SELECT ticker, side, avg_price, contracts, pnl "
                        "FROM positions WHERE status='open'"
                    ) or []
                    if open_pos:
                        lines.append(f"Currently open positions ({len(open_pos)}):")
                        for p in open_pos:
                            lines.append(
                                f"  {p.get('ticker','')} {(p.get('side') or '').upper()} "
                                f"| entry={p.get('avg_price',0):.0f}¢ | {p.get('contracts',0)} contracts "
                                f"| unrealised PnL=${p.get('pnl') or 0:+.2f}"
                            )
                    else:
                        lines.append("Currently open positions: None")

                except Exception:
                    pass

            # Daily stats
            lines.append(
                f"Today: {snap.get('markets_scanned',0):,} markets scanned | "
                f"{snap.get('signals_generated',0)} BUY signals | "
                f"{snap.get('trades_executed',0)} trades placed | "
                f"{snap.get('trades_skipped',0)} skipped"
            )

            # Consecutive losses warning
            consec = snap.get("consecutive_losses", 0)
            max_consec = settings.trading.max_consecutive_losses
            if consec > 0:
                lines.append(
                    f"⚠️ Consecutive losses: {consec} (lockout triggers at {max_consec}) — "
                    f"{'be cautious' if consec >= max_consec // 2 else 'within normal range'}"
                )

            lines.append("=== END BOT SELF-AWARENESS ===")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_prompt(self, market: Dict, signals: List[Dict], context: str = "", bot_context: str = "") -> str:
        ticker        = market.get("ticker", "")
        title         = market.get("title", "")
        # Use last_price as fallback when order book is thin (yes_ask=0)
        _last         = float(market.get("last_price", 0) or 0)
        yes_ask       = float(market.get("yes_ask", 0) or 0) or _last
        no_ask        = float(market.get("no_ask",  0) or 0) or (100 - _last if _last else 0)
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

        # Pre-compute EV helpers
        yes_ev_if_true = (100 - yes_ask) * 0.98
        no_ev_if_true  = (100 - no_ask)  * 0.98
        spread         = yes_ask + no_ask - 100
        liquidity      = "LOW" if volume < 100 else "HIGH" if volume > 10000 else "MEDIUM"
        # Warn AI if no live price data was fetched for asset markets
        _no_price_warn = ""
        if not context and any(k in title.lower() for k in ["bitcoin", "btc", "eth", "crypto", "price", "$"]):
            _no_price_warn = "\n⚠️ WARNING: Live price fetch failed — your training data prices may be STALE. Do NOT cite specific prices unless you are certain they are current. Be extra conservative."
        context_block  = f"\n\n--- REAL-WORLD CONTEXT (live data fetched right now) ---\n{context}\n--- END CONTEXT ---" if context else _no_price_warn
        bot_block      = f"\n\n{bot_context}" if bot_context else ""
        no_context_warning = "\n⚠️ NO LIVE DATA AVAILABLE — default confidence to ≤ 50. Do NOT guess." if not context else ""

        return f"""You are a professional prediction market trader managing real money. Your job is to find high-conviction bets backed by real-world evidence — NOT to trade for the sake of trading.

GOLDEN RULE: A confident HOLD is always better than a weak BUY. Cash is a position.
MINIMUM BAR: Only BUY when you have specific, verifiable facts that justify your probability estimate.
TARGET: Reach confidence 75–100 only when real-world data clearly supports it. Never inflate confidence.

=== MARKET ===
Ticker:   {ticker}
Question: {title}
Closes:   {close_time}

=== CURRENT MARKET PRICES ===
YES ask: {yes_ask:.0f}¢  → market implies {yes_ask:.0f}% chance of YES
NO ask:  {no_ask:.0f}¢  → market implies {no_ask:.0f}% chance of NO
Spread:  {spread:.0f}¢  |  Volume: {volume:,}  |  Liquidity: {liquidity}
{poly_block}
=== EXPECTED VALUE (per contract, after 2% Kalshi fee) ===
BUY YES @ {yes_ask:.0f}¢ → profit {yes_ev_if_true:.1f}¢ if YES resolves  |  lose {yes_ask:.0f}¢ if NO  |  break-even = {yes_ask/98*100:.1f}% true prob
BUY NO  @ {no_ask:.0f}¢ → profit {no_ev_if_true:.1f}¢ if NO resolves   |  lose {no_ask:.0f}¢ if YES |  break-even = {no_ask/98*100:.1f}% true prob
{arb_text}{context_block}{bot_block}{no_context_warning}

=== HOW TO SCORE CONFIDENCE ===
90–100% → You have direct, current, unambiguous data (live score, official result, real-time price)
75–89%  → Strong evidence from multiple sources clearly pointing one way
60–74%  → Good evidence but some uncertainty remains
50–59%  → Weak evidence or conflicting signals — lean HOLD
< 50%   → No real edge — always HOLD

=== YOUR TASK ===
Step 1 — Read the REAL-WORLD CONTEXT carefully. Extract every specific fact relevant to this question.
Step 2 — Estimate TRUE P(YES) based ONLY on those facts. If context is missing, default to 50%.
Step 3 — Check Polymarket price (if shown) — large gaps between platforms signal mispricing.
Step 4 — Compute net EV = (true_prob/100 - market_price/100) × 98¢ for the better side.
Step 5 — BUY only if: net_ev > 2¢ AND confidence ≥ {self.trading_cfg.min_ai_confidence:.0f}% AND you can cite specific facts.
Step 6 — HOLD if: no strong evidence, conflicting data, context missing, or EV is marginal.

Respond ONLY with this exact JSON (no markdown):
{{
  "true_prob_yes": <your estimated 0-100 probability that YES resolves>,
  "action": "BUY" | "HOLD",
  "side": "yes" | "no" | null,
  "net_ev_cents": <expected profit per contract after fee, negative = unfavourable>,
  "confidence": <integer 0-100 — how certain you are in your true_prob, based on evidence quality>,
  "reasoning": "<3-4 sentences. Quote specific facts from context. State what evidence drove your estimate and what would change your mind.>"
}}

HARD RULES — violating these means a bad trade:
- confidence must reflect EVIDENCE QUALITY, not how exciting the trade looks
- No context available = confidence ≤ 50, action = HOLD
- If Polymarket and Kalshi agree on price, there's likely no edge — be skeptical
- Minimum net_ev to BUY = 2¢ (small edges get eaten by variance)
- confidence ≥ {self.trading_cfg.min_ai_confidence:.0f} required — if you can't reach it, HOLD and say why"""

    async def decide(self, market: Dict, signals: List[Dict] = []) -> AIDecision:
        ticker = market.get("ticker", "UNKNOWN")

        if not self.cfg.openai_api_key:
            logger.warning("No OPENAI_API_KEY set — using rule-based fallback")
            return self._rule_based_decision(market, signals)

        try:
            from src.data.context_builder import build_market_context
            is_live = market.get("is_live") or market.get("platform") == "polymarket"
            context = await build_market_context(market, timeout_seconds=12.0 if is_live else 8.0)
        except Exception:
            context = ""

        # Build bot self-awareness context — let AI know its own state
        bot_context = await self._build_bot_context()

        prompt = self._build_prompt(market, signals, context, bot_context)
        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.cfg.model,
                max_tokens=self.cfg.max_tokens,
                temperature=self.cfg.temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                logger.warning("AI returned empty response for %s — rule-based fallback", ticker)
                return self._rule_based_decision(market, signals)
            # Strip markdown fences if model wraps JSON
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw.strip())

            action      = data.get("action", "HOLD").upper()
            side        = (data.get("side") or "yes").lower()
            confidence  = max(0.0, min(float(data.get("confidence", 0)), 100.0))
            reasoning   = str(data.get("reasoning", ""))[:500]
            true_prob   = data.get("true_prob_yes")
            net_ev      = data.get("net_ev_cents")

            # Sanity clamps — physically impossible values indicate hallucination
            if true_prob is not None:
                true_prob = max(0.0, min(float(true_prob), 100.0))
            if net_ev is not None:
                net_ev = float(net_ev)
                # Max possible net EV on any contract is (100-1)*0.98 = 97.02¢
                net_ev = max(-100.0, min(net_ev, 97.0))

            # Validate side is recognised
            if side not in ("yes", "no"):
                side = "yes"

            # Reject low/negative EV — need at least 2¢ edge to overcome variance
            if action == "BUY" and net_ev is not None and net_ev < 2.0:
                action = "HOLD"
                reasoning = f"[EV guard: net_ev={net_ev:.1f}¢ < 2¢ minimum] " + reasoning

            # Reject physically impossible EV given the market price
            if action == "BUY" and net_ev is not None:
                _yes_ask = float(market.get("yes_ask", 50))
                _no_ask  = float(market.get("no_ask", 50))
                price_for_side = _yes_ask if side == "yes" else _no_ask
                max_possible_ev = (100.0 - price_for_side) * 0.98
                if net_ev > max_possible_ev + 1.0:
                    logger.warning(
                        "[AI SANITY] %s net_ev=%.1f¢ > physical max=%.1f¢ — downgrading to HOLD",
                        ticker, net_ev, max_possible_ev,
                    )
                    action = "HOLD"
                    reasoning = f"[Sanity: impossible EV {net_ev:.1f}¢ > max {max_possible_ev:.1f}¢] " + reasoning

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

            # Persist decision — gpt-4o-mini pricing: $0.15/M input, $0.60/M output
            if self.db:
                now = datetime.now(timezone.utc).isoformat()
                try:
                    usage = response.usage
                    cost = (usage.prompt_tokens * 0.15e-6 + usage.completion_tokens * 0.60e-6)
                    await self.db.insert("ai_decisions", {
                        "ticker": ticker,
                        "action": action,
                        "confidence": confidence,
                        "reasoning": reasoning,
                        "model": self.cfg.model,
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "cost_usd": cost,
                        "decided_at": now,
                    })
                except Exception:
                    pass

            return decision

        except json.JSONDecodeError as e:
            logger.warning("AI JSON parse error for %s: %s — raw: %s", ticker, e, raw[:200] if 'raw' in dir() else "?")
            return self._rule_based_decision(market, signals)
        except Exception as e:
            err_str = str(e)
            if "insufficient_quota" in err_str or "billing" in err_str.lower():
                raise
            if "model" in err_str.lower() or "not_found" in err_str.lower():
                logger.error("AI MODEL ERROR for %s — check AI_MODEL env var: %s", ticker, e)
            else:
                logger.error("AI decision error for %s: %s", ticker, e)
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

        # Low volume — skip but don't hard-block; let AI have the final word
        if volume < 100:
            return AIDecision(action="HOLD", confidence=40.0,
                              reasoning="Insufficient volume for confident assessment",
                              model="rule_based", ticker=ticker)

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

        if not self.cfg.openai_api_key:
            return {"verdict": "HOLD", "confidence": 50, "reasoning": "No AI key — holding by default"}

        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self.cfg.model,
                max_tokens=512,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = (response.choices[0].message.content or "").strip()
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
            logger.warning("Re-eval error for %s: %s", ticker, e)
            return {"verdict": "HOLD", "confidence": 0, "reasoning": f"Re-eval failed: {e}"}
