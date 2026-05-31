"""Phase 9 — Discord webhook alerts for trades, signals, and errors."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.discord")


class DiscordAlerter:
    def __init__(self):
        from src.config.settings import settings
        self.cfg = settings.alerts
        self.webhook_url = self.cfg.discord_webhook_url
        self.enabled = self.cfg.discord_enabled

    async def _post(self, payload: Dict) -> bool:
        if not self.enabled or not self.webhook_url:
            logger.debug("Discord not configured — skipping alert")
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.warning(f"Discord alert failed: {e}")
            return False

    def _embed(self, title: str, description: str, color: int,
               fields: Optional[List[Dict]] = None) -> Dict:
        embed: Dict = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if fields:
            embed["fields"] = fields
        return {"embeds": [embed]}

    # ── Alert methods ─────────────────────────────────────────────────────────

    async def test_alert(self, mode: str = "PAPER") -> bool:
        """Send a connectivity test message. Returns True if delivered."""
        payload = self._embed(
            title="✅ Kalshi Bot — Connection Test",
            description=(
                f"Discord webhook is working!\n"
                f"Mode: **{mode}**\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            ),
            color=0x00BFFF,
            fields=[
                {"name": "Status", "value": "Online", "inline": True},
                {"name": "Alerts", "value": "Enabled", "inline": True},
            ],
        )
        ok = await self._post(payload)
        if ok:
            logger.info("Discord test alert delivered successfully")
        else:
            logger.warning("Discord test alert failed — check DISCORD_WEBHOOK_URL in .env")
        return ok

    async def startup_banner(self, mode: str, balance: Optional[float] = None,
                              poly_enabled: bool = False) -> None:
        """Send bot startup notification."""
        from src.config.settings import settings
        poly_on = poly_enabled or settings.polymarket.enabled
        color = 0xFF4444 if mode == "LIVE" else 0x00FF7F
        platforms = "🟦 Kalshi + 🟣 Polymarket" if poly_on else "🟦 Kalshi"
        fields = [
            {"name": "Trading Mode", "value": f"**{mode}**", "inline": True},
            {"name": "Platforms",    "value": platforms,      "inline": True},
        ]
        if balance is not None:
            fields.append({"name": "Account Balance", "value": f"${balance:.2f}", "inline": True})
        payload = self._embed(
            title=f"🚀 Kalshi + Polymarket Bot Started — {mode} MODE",
            description=f"Bot is online and scanning markets on {platforms}.",
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def trade_executed(self, ticker: str, action: str, side: str,
                              price: float, contracts: int, size_dollars: float,
                              pnl: Optional[float], ai_confidence: Optional[float],
                              paper: bool = True, signal_source: str = "",
                              reasoning: str = "", net_ev: Optional[float] = None,
                              exp_profit: Optional[float] = None,
                              market_title: str = "") -> None:
        if not self.cfg.alert_on_trade:
            return
        color    = 0x00FF00 if action == "BUY" else 0xFF4444
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"

        # Source badge
        if signal_source in ("internal_arb", "cross_market_arb"):
            source_emoji = "⚡"
        elif signal_source == "rule_based":
            source_emoji = "📐"
        else:
            source_emoji = "🤖"

        # Max payout if position resolves in our favour
        max_payout = contracts * (100 - price) / 100

        fields = [
            {"name": "Side",      "value": f"**{side.upper()}**",       "inline": True},
            {"name": "Price",     "value": f"{price:.0f}¢",             "inline": True},
            {"name": "Contracts", "value": str(contracts),              "inline": True},
            {"name": "Capital",   "value": f"${size_dollars:.2f}",      "inline": True},
            {"name": "Max Payout","value": f"${max_payout:.2f}",        "inline": True},
        ]
        if ai_confidence is not None:
            fields.append({"name": "AI Confidence", "value": f"{ai_confidence:.0f}%", "inline": True})
        if net_ev is not None:
            fields.append({"name": "Net EV / contract", "value": f"{net_ev:.1f}¢",    "inline": True})
        if exp_profit is not None:
            fields.append({"name": "Exp. Profit",  "value": f"${exp_profit:.2f}",     "inline": True})
        if pnl is not None:
            fields.append({"name": "PnL", "value": f"${pnl:+.2f}", "inline": True})
        if reasoning:
            fields.append({"name": f"{source_emoji} AI Reasoning", "value": reasoning[:300], "inline": False})

        title_line = f"\n_{market_title[:80]}_" if market_title else ""
        payload = self._embed(
            title=f"{source_emoji} {mode_tag} Trade Entered — {ticker}",
            description=f"**{action} {side.upper()}** on `{ticker}`{title_line}",
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def arb_signal(self, ticker: str, signal_type: str,
                          gross_edge: float, net_edge: float,
                          side: str = "", kalshi_price: float = 0,
                          poly_price: float = 0) -> None:
        """Alert for arbitrage signal detected (only if ALERT_ON_SIGNAL=true)."""
        if not self.cfg.alert_on_signal:
            return
        if signal_type == "internal_arb":
            desc = (
                f"**Internal arb** on `{ticker}`\n"
                f"YES + NO = {kalshi_price + poly_price:.0f}¢ (should be 100¢)\n"
                f"Gross edge: **{gross_edge:.1f}¢** | Net after fees: **{net_edge:.1f}¢**"
            )
        else:
            desc = (
                f"**Cross-market arb** on `{ticker}`\n"
                f"Kalshi={kalshi_price:.0f}¢  Poly={poly_price:.0f}¢\n"
                f"Buy **{side.upper()}** on Kalshi | Net edge: **{net_edge:.1f}¢**"
            )
        payload = self._embed(
            title=f"📡 Arb Signal — {ticker}",
            description=desc,
            color=0xFFAA00,
        )
        await self._post(payload)

    async def signal_detected(self, ticker: str, signal_type: str,
                               diff_pct: float, edge_cents: float) -> None:
        """Generic signal alert (backward compat)."""
        if not self.cfg.alert_on_signal:
            return
        payload = self._embed(
            title=f"📡 Signal: {ticker}",
            description=f"Type: **{signal_type}** | Diff: **{diff_pct:.1f}%** | Edge: **{edge_cents:.1f}¢**",
            color=0xFFAA00,
        )
        await self._post(payload)

    async def error_alert(self, error_msg: str, context: str = "") -> None:
        if not self.cfg.alert_on_error:
            return
        payload = self._embed(
            title="⚠️ Bot Error",
            description=f"```{error_msg[:1000]}```",
            color=0xFF0000,
            fields=[{"name": "Context", "value": context[:200], "inline": False}] if context else None,
        )
        await self._post(payload)

    async def position_closed(self, ticker: str, side: str, contracts: int,
                               entry_cents: float, exit_cents: float,
                               pnl: float, reason: str, paper: bool = True,
                               market_result: str = "", market_title: str = "") -> None:
        """Alert when any position is closed (stop-loss, take-profit, resolved, AI opt-out)."""
        if not self.cfg.alert_on_trade:
            return
        pnl_sign = "+" if pnl >= 0 else ""
        color    = 0x00FF00 if pnl >= 0 else 0xFF4444
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"

        # Human-readable trigger label
        if reason.startswith("resolved"):
            result     = market_result or reason.split(":")[-1].strip()
            won        = (side.lower() == result.lower()) if result else (pnl >= 0)
            outcome    = "WON ✅" if won else "LOST ❌"
            result_str = result.upper() if result else "?"
            trigger_emoji  = "✅" if won else "❌"
            trigger_label  = f"Market Resolved {result_str} — You {outcome}"
        elif reason.startswith("stop_loss"):
            trigger_emoji, trigger_label = "🛑", "Stop-Loss Triggered"
        elif reason.startswith("take_profit"):
            trigger_emoji, trigger_label = "🎯", "Take-Profit Hit"
        elif reason.startswith("ai_reeval"):
            trigger_emoji, trigger_label = "🤖", "AI Opted Out"
            ai_reason = reason[len("ai_reeval:"):].strip()
        else:
            trigger_emoji, trigger_label = "🔒", reason

        fields = [
            {"name": "Side",      "value": side.upper(),                  "inline": True},
            {"name": "Contracts", "value": str(contracts),                "inline": True},
            {"name": "Entry",     "value": f"{entry_cents:.0f}¢",         "inline": True},
            {"name": "Exit",      "value": f"{exit_cents:.0f}¢",          "inline": True},
            {"name": "PnL",       "value": f"${pnl_sign}{abs(pnl):.2f}",  "inline": True},
            {"name": "Trigger",   "value": f"{trigger_emoji} {trigger_label}", "inline": True},
        ]

        # For AI opt-out, add the reasoning as its own field so it's readable
        if reason.startswith("ai_reeval") and ai_reason:
            fields.append({
                "name":   "AI Reasoning",
                "value":  ai_reason[:300],
                "inline": False,
            })

        title_line = f"\n_{market_title[:80]}_" if market_title else ""
        if reason.startswith("resolved"):
            desc = (
                f"**Prediction: {side.upper()}** on `{ticker}`{title_line}\n"
                f"Market resolved **{result_str}** — **{outcome}**\n"
                f"PnL: **${pnl_sign}{abs(pnl):.2f}**"
            )
        else:
            desc = (
                f"Closed **{side.upper()}** position on `{ticker}`{title_line}\n"
                f"PnL: **${pnl_sign}{abs(pnl):.2f}**  ({trigger_label})"
            )

        payload = self._embed(
            title=f"{trigger_emoji} {mode_tag} Position Closed — {ticker}",
            description=desc,
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def best_opportunity_found(
        self, ticker: str, side: str, price_cents: float,
        confidence: float, net_ev: Optional[float], exp_profit: Optional[float],
        score: float, reasoning: str,
        poly_yes: Optional[float] = None, poly_no: Optional[float] = None,
        market_title: str = "", paper: bool = True, platform: str = "kalshi",
    ) -> None:
        """Alert fired BEFORE placing the trade — 'here's the best opportunity we found today'."""
        if not self.cfg.alert_on_trade:
            return
        platform_tag = "🟣 POLYMARKET" if platform == "polymarket" else "🟦 KALSHI"
        mode_tag   = "📝 PAPER" if paper else "💰 LIVE"
        score_pct  = f"{score * 100:.1f}"
        ev_str     = f"{net_ev:.1f}¢" if net_ev is not None else "n/a"
        profit_str = f"${exp_profit:.2f}" if exp_profit is not None else "n/a"

        fields = [
            {"name": "Side",         "value": f"**{side.upper()}**",  "inline": True},
            {"name": "Price",        "value": f"{price_cents:.0f}¢",  "inline": True},
            {"name": "Confidence",   "value": f"{confidence:.0f}%",   "inline": True},
            {"name": "Net EV",       "value": ev_str,                  "inline": True},
            {"name": "Exp. Profit",  "value": profit_str,              "inline": True},
            {"name": "Opp. Score",   "value": f"{score_pct}/100",      "inline": True},
        ]
        if poly_yes is not None and poly_no is not None:
            fields.append({
                "name":  "Polymarket Cross-Check",
                "value": f"YES {poly_yes:.0f}¢  |  NO {poly_no:.0f}¢",
                "inline": False,
            })
        if reasoning:
            fields.append({
                "name":  "🤖 Why this trade",
                "value": reasoning[:300],
                "inline": False,
            })

        fields.insert(0, {"name": "Platform", "value": platform_tag, "inline": True})
        title_line = f"\n_{market_title[:80]}_" if market_title else ""
        payload = self._embed(
            title=f"🎯 {mode_tag} Best Opportunity Found — {ticker}",
            description=(
                f"**Placing bet: BUY {side.upper()} on `{ticker}`** {platform_tag}{title_line}\n"
                f"Scanned Kalshi + Polymarket — this is today's best edge."
            ),
            color=0x00BFFF,
            fields=fields,
        )
        await self._post(payload)

    async def no_opportunity(self, markets_scanned: int, paper: bool = True) -> None:
        """Alert when the bot scans everything and finds nothing worth trading."""
        if not self.cfg.alert_on_signal:
            return
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        payload  = self._embed(
            title=f"💤 {mode_tag} No Opportunity Today",
            description=(
                f"Scanned {markets_scanned} markets across Kalshi + Polymarket.\n"
                f"Nothing cleared the confidence + profit threshold.\n"
                f"**Sitting out — cash is a valid position.**"
            ),
            color=0x808080,
        )
        await self._post(payload)

    async def ai_reeval_hold(self, ticker: str, side: str, pct_change: float,
                              reasoning: str, paper: bool = True) -> None:
        """Alert when AI re-evaluates a position and decides to HOLD (optional — only if ALERT_ON_SIGNAL)."""
        if not self.cfg.alert_on_signal:
            return
        color    = 0x5865F2   # Discord blurple — neutral
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        payload  = self._embed(
            title=f"🤖 {mode_tag} AI Re-eval: HOLD — {ticker}",
            description=(
                f"AI reviewed `{ticker}` ({side.upper()}) and decided to **HOLD**.\n"
                f"Unrealised: **{pct_change:+.1f}%**\n\n"
                f"_{reasoning[:300]}_"
            ),
            color=color,
        )
        await self._post(payload)

    async def daily_summary(self, date: str, trades: int, capital: float,
                             pnl: float, open_positions: int, paper: bool = True) -> None:
        """Send daily recap every evening regardless of activity."""
        mode_tag  = "📝 PAPER" if paper else "💰 LIVE"
        pnl_sign  = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        color     = 0x00FF00 if pnl >= 0 else 0xFF4444
        status    = "Bot is alive and running ✅" if trades >= 0 else "Check bot status ⚠️"
        payload   = self._embed(
            title=f"📊 {mode_tag} Daily Summary — {date}",
            description=f"{status}\nScanning **Kalshi + Polymarket** 24/7 in paper mode.",
            color=color,
            fields=[
                {"name": "Trades Today",     "value": str(trades),                          "inline": True},
                {"name": "Capital Deployed", "value": f"${capital:.2f}",                    "inline": True},
                {"name": f"{pnl_emoji} PnL", "value": f"${pnl_sign}{pnl:.2f}",             "inline": True},
                {"name": "Open Positions",   "value": str(open_positions),                  "inline": True},
                {"name": "Mode",             "value": "Paper (no real money)",               "inline": True},
                {"name": "Next Summary",     "value": "Tomorrow 8PM UTC",                   "inline": True},
            ],
        )
        await self._post(payload)

    async def pnl_update(self, total_pnl: float, win_rate: float,
                          total_trades: int, scale_factor: float) -> None:
        color = 0x00FF00 if total_pnl >= 0 else 0xFF4444
        payload = self._embed(
            title="📊 Performance Update",
            description=f"Total PnL: **${total_pnl:+.2f}**",
            color=color,
            fields=[
                {"name": "Win Rate", "value": f"{win_rate:.1f}%", "inline": True},
                {"name": "Total Trades", "value": str(total_trades), "inline": True},
                {"name": "Scale Factor", "value": f"{scale_factor:.2f}x", "inline": True},
            ],
        )
        await self._post(payload)
