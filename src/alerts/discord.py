"""Phase 9 — Discord webhook alerts for trades, signals, and errors."""

import asyncio
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
            async def _send():
                async with httpx.AsyncClient(timeout=4) as client:
                    resp = await client.post(self.webhook_url, json=payload)
                    resp.raise_for_status()
                    return True
            return await asyncio.wait_for(_send(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Discord alert timed out (>5s) — trade cycle unaffected")
            return False
        except Exception as e:
            logger.warning("Discord alert failed: %s", e)
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
        ai_reason = ""

        # Human-readable trigger label + plain-English explanation
        if reason.startswith("resolved"):
            result        = market_result or reason.split(":")[-1].strip()
            won           = (side.lower() == result.lower()) if result else (pnl >= 0)
            outcome       = "WON ✅" if won else "LOST ❌"
            result_str    = result.upper() if result else "?"
            trigger_emoji = "✅" if won else "❌"
            trigger_label = f"Market Resolved — {outcome}"
            explanation   = (
                f"The market officially resolved **{result_str}**. "
                f"You bet **{side.upper()}** so you **{'won' if won else 'lost'}**."
            )
        elif reason.startswith("stop_loss"):
            pct = reason.split(":")[-1].strip() if ":" in reason else ""
            trigger_emoji = "🛑"
            trigger_label = "Stop-Loss Triggered"
            explanation   = (
                f"The price dropped **{pct}** from your entry. "
                f"The bot cut the loss automatically to protect your capital. "
                f"Better to take a small loss now than a bigger one later."
            )
        elif reason.startswith("take_profit"):
            pct = reason.split(":")[-1].strip() if ":" in reason else ""
            trigger_emoji = "🎯"
            trigger_label = "Take-Profit Hit"
            explanation   = (
                f"The price moved **{pct}** in your favour. "
                f"The bot locked in the profit automatically."
            )
        elif reason.startswith("ai_reeval"):
            ai_reason     = reason[len("ai_reeval:"):].strip()
            trigger_emoji = "🤖"
            trigger_label = "AI Opted Out — Bad Trade Detected"
            explanation   = (
                f"The AI re-analysed this position with fresh real-world data and "
                f"determined the original bet no longer makes sense. "
                f"It exited early to limit losses."
            )
        else:
            trigger_emoji = "🔒"
            trigger_label = reason
            explanation   = ""

        fields = [
            {"name": "Question",  "value": (market_title or ticker)[:80], "inline": False},
            {"name": "Your Bet",  "value": f"**{side.upper()}**",          "inline": True},
            {"name": "Contracts", "value": str(contracts),                 "inline": True},
            {"name": "Entry",     "value": f"{entry_cents:.0f}¢",          "inline": True},
            {"name": "Exit",      "value": f"{exit_cents:.0f}¢",           "inline": True},
            {"name": "Result",    "value": f"**${pnl_sign}{abs(pnl):.2f}**", "inline": True},
            {"name": "Why",       "value": f"{trigger_emoji} {trigger_label}", "inline": True},
        ]
        if explanation:
            fields.append({"name": "📖 Plain English", "value": explanation, "inline": False})
        if ai_reason:
            fields.append({"name": "🤖 AI's Exact Reasoning", "value": ai_reason[:300], "inline": False})

        payload = self._embed(
            title=f"{trigger_emoji} {mode_tag} Position Closed — {'Profit' if pnl >= 0 else 'Loss'} ${pnl_sign}{abs(pnl):.2f}",
            description=f"`{ticker}` · **{side.upper()}** · {contracts} contracts",
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

        title_line = market_title[:100] if market_title else ticker
        poly_check = ""
        if poly_yes is not None and poly_no is not None:
            poly_check = (
                f"\nPolymarket agrees: YES {poly_yes:.0f}¢ / NO {poly_no:.0f}¢ — "
                f"cross-platform confirmation of the edge."
            )

        fields = [
            {"name": "❓ Question",      "value": title_line,             "inline": False},
            {"name": "🎲 Bet",           "value": f"**BUY {side.upper()}**", "inline": True},
            {"name": "💲 Price",         "value": f"{price_cents:.0f}¢",  "inline": True},
            {"name": "🎯 Confidence",    "value": f"{confidence:.0f}%",   "inline": True},
            {"name": "📈 Expected Profit", "value": profit_str,           "inline": True},
            {"name": "⚖️ Edge per contract", "value": ev_str,            "inline": True},
            {"name": "🏦 Platform",      "value": platform_tag,           "inline": True},
        ]
        if reasoning:
            fields.append({
                "name":  "🤖 Why the AI is placing this trade",
                "value": reasoning[:400],
                "inline": False,
            })

        payload = self._embed(
            title=f"🎯 {mode_tag} Trade Placed — BUY {side.upper()} on {ticker}",
            description=(
                f"The AI found a profitable edge and is placing a bet.{poly_check}\n\n"
                f"**If this resolves {side.upper()}, you profit. If not, you lose your stake.**"
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

    async def near_miss(self, ticker: str, side: str, confidence: float,
                        min_confidence: float, net_ev: Optional[float],
                        true_prob: Optional[float], reasoning: str,
                        paper: bool = True) -> None:
        """Alert when AI almost pulls the trigger but confidence fell just short."""
        if not self.cfg.alert_on_signal:
            return
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        ev_str   = f"{net_ev:+.1f}¢" if net_ev is not None else "n/a"
        tp_str   = f"{true_prob:.0f}%" if true_prob is not None else "n/a"
        gap      = min_confidence - confidence
        payload  = self._embed(
            title=f"🟡 {mode_tag} Near-Miss — {ticker}",
            description=(
                f"AI almost traded `{ticker}` ({side.upper()}) but confidence fell short.\n\n"
                f"**Confidence:** {confidence:.0f}%  _(need {min_confidence:.0f}% — gap: {gap:.0f}%)_\n"
                f"**Net EV:** {ev_str}  |  **P(YES):** {tp_str}\n\n"
                f"_{reasoning[:300]}_"
            ),
            color=0xFFAA00,
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
                             pnl: float, open_positions: int, paper: bool = True,
                             closed_trades: Optional[List[Dict]] = None) -> None:
        """Send daily recap every evening regardless of activity."""
        mode_tag  = "📝 PAPER" if paper else "💰 LIVE"
        pnl_sign  = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        color     = 0x00FF00 if pnl >= 0 else 0xFF4444
        status    = "Bot is alive and running ✅"

        fields = [
            {"name": "Trades Today",        "value": str(trades),                 "inline": True},
            {"name": "Capital Deployed",    "value": f"${capital:.2f}",           "inline": True},
            {"name": f"{pnl_emoji} PnL",    "value": f"${pnl_sign}{pnl:.2f}",    "inline": True},
            {"name": "Open Positions",      "value": str(open_positions),         "inline": True},
            {"name": "Mode",                "value": "Paper (no real money)" if paper else "LIVE",  "inline": True},
            {"name": "Next Summary",        "value": "Tomorrow 8PM UTC",          "inline": True},
        ]

        # Append each closed trade's result so you can review them
        if closed_trades:
            trade_lines = []
            for t in closed_trades[:10]:
                t_pnl  = t.get("pnl", 0) or 0
                sign   = "+" if t_pnl >= 0 else ""
                icon   = "✅" if t_pnl >= 0 else "❌"
                why    = t.get("close_reason", "")
                label  = (
                    "resolved" if why.startswith("resolved") else
                    "stop-loss" if why.startswith("stop_loss") else
                    "take-profit" if why.startswith("take_profit") else
                    "AI opt-out" if why.startswith("ai_reeval") else why
                )
                trade_lines.append(
                    f"{icon} `{t.get('ticker','')}` {t.get('side','').upper()} — "
                    f"**${sign}{t_pnl:.2f}** ({label})"
                )
            fields.append({
                "name":   "📋 Today's Closed Trades",
                "value":  "\n".join(trade_lines) or "None",
                "inline": False,
            })

        payload   = self._embed(
            title=f"📊 {mode_tag} Daily Summary — {date}",
            description=f"{status}\nScanning **Kalshi + Polymarket** 24/7.",
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def hourly_heartbeat(
        self,
        markets_scanned: int,
        kalshi_count: int,
        poly_count: int,
        top_candidates: list,
        open_positions: int,
        paper_pnl: float,
        paper: bool = True,
        closed_trades: Optional[List[Dict]] = None,
        win_rate: float = 0.0,
        total_wins: int = 0,
        total_losses: int = 0,
        total_pnl: float = 0.0,
        total_closed: int = 0,
    ) -> None:
        """Send hourly scan summary — bot heartbeat + top market candidates."""
        now_utc  = datetime.now(timezone.utc)
        hhmm     = now_utc.strftime("%H:%M")
        pnl_sign = "+" if paper_pnl >= 0 else ""
        color    = 0x5865F2  # Discord blurple — neutral heartbeat colour

        # Build watching field value
        if top_candidates:
            lines = []
            for i, c in enumerate(top_candidates[:3], 1):
                ticker   = c.get("ticker", "?")
                title    = (c.get("title") or "")[:40]
                yes_ask  = c.get("yes_ask", 0)
                no_ask   = c.get("no_ask",  0)
                volume   = c.get("volume",  0)
                platform = c.get("platform", "kalshi")
                plat_tag = "🟣" if platform == "polymarket" else "🟦"
                lines.append(
                    f"{i}. {plat_tag} `{ticker}` — {title}\n"
                    f"   YES {yes_ask:.0f}¢ | NO {no_ask:.0f}¢ | vol {volume:,}"
                )
            watching_value = "\n".join(lines)
        else:
            watching_value = "_No candidates above threshold_"

        # Win rate display
        if total_closed == 0:
            record_str = "No trades yet — building track record..."
            wr_emoji   = "🆕"
        else:
            wr_emoji   = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"
            all_pnl_sign = "+" if total_pnl >= 0 else ""
            record_str = (
                f"**{win_rate:.0f}% win rate** — "
                f"{total_wins}W / {total_losses}L / {total_closed} total | "
                f"All-time PnL: **${all_pnl_sign}{total_pnl:.2f}**"
            )

        fields = [
            {
                "name":   f"{wr_emoji} Bot Track Record (Can I Trust It?)",
                "value":  record_str,
                "inline": False,
            },
            {
                "name":   "Markets Scanned",
                "value":  f"{kalshi_count} Kalshi + {poly_count} Polymarket = **{markets_scanned} total**",
                "inline": False,
            },
            {
                "name":   "Open Positions",
                "value":  f"{open_positions} | Today's PnL: **${pnl_sign}{paper_pnl:.2f}**",
                "inline": False,
            },
            {
                "name":   "👀 Watching — Top Candidates",
                "value":  watching_value,
                "inline": False,
            },
            {
                "name":   "Next Scan",
                "value":  "in ~60s",
                "inline": False,
            },
        ]

        # Show today's trade results if any
        if closed_trades:
            lines = []
            for t in closed_trades[:8]:
                t_pnl = t.get("pnl") or 0
                sign  = "+" if t_pnl >= 0 else ""
                icon  = "✅" if t_pnl >= 0 else "❌"
                why   = t.get("close_reason", "")
                label = (
                    "resolved" if why.startswith("resolved") else
                    "stop-loss" if why.startswith("stop_loss") else
                    "take-profit" if why.startswith("take_profit") else
                    "AI opt-out" if why.startswith("ai_reeval") else "closed"
                )
                lines.append(f"{icon} `{t.get('ticker','')}` {(t.get('side') or '').upper()} **${sign}{t_pnl:.2f}** — {label}")
            fields.insert(-1, {
                "name":   "📋 Today's Trade Results",
                "value":  "\n".join(lines),
                "inline": False,
            })

        payload = self._embed(
            title=f"🔍 Hourly Scan Report — {hhmm} UTC",
            description="Bot alive ✅ | Scanning Kalshi + Polymarket every 60s",
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def midnight_daily_summary(
        self,
        date: str,
        snap: dict,
        wins: int,
        losses: int,
        total_closed: int,
        alltime_pnl: float,
        today_pnl: float,
        open_positions: int,
        closed_today: list,
        paper: bool = True,
    ) -> None:
        """Midnight daily report — full day recap posted to Discord."""
        mode_tag  = "📝 PAPER" if paper else "💰 LIVE"
        pnl_sign  = "+" if today_pnl >= 0 else ""
        all_sign  = "+" if alltime_pnl >= 0 else ""
        color     = 0x00FF7F if today_pnl >= 0 else 0xFF4444
        win_rate  = (wins / total_closed * 100) if total_closed > 0 else 0.0
        wr_emoji  = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴" if total_closed > 0 else "🆕"
        err_count = len(snap.get("errors", []))

        fields = [
            {
                "name":  "🤖 Bot Status",
                "value": (
                    f"Uptime: **{snap.get('uptime','?')}** | Mode: {mode_tag}\n"
                    f"Running 24/7 — scanning Kalshi + Polymarket continuously."
                ),
                "inline": False,
            },
            {
                "name":  "📊 Today's Activity",
                "value": (
                    f"Markets scanned: **{snap.get('markets_scanned', 0):,}**\n"
                    f"AI signals generated: **{snap.get('signals_generated', 0)}** (BUY recommendations before risk gates)\n"
                    f"Trades executed: **{snap.get('trades_executed', 0)}**\n"
                    f"Trades skipped: **{snap.get('trades_skipped', 0)}** (risk / profit gate / duplicate)"
                ),
                "inline": False,
            },
            {
                "name":  "💰 Performance",
                "value": (
                    f"Today's PnL: **${pnl_sign}{today_pnl:.2f}**\n"
                    f"All-time PnL: **${all_sign}{alltime_pnl:.2f}**\n"
                    f"{wr_emoji} Win rate: **{win_rate:.0f}%** ({wins}W / {losses}L / {total_closed} total)\n"
                    f"Open positions: **{open_positions}**"
                ),
                "inline": False,
            },
            {
                "name":  "🔗 Polymarket ↔ Kalshi Matching",
                "value": (
                    f"Matched pairs today: **{snap.get('poly_matches', 0)}**\n"
                    f"Suspicious / low-confidence matches flagged: **{len(snap.get('suspicious_matches', []))}**"
                    + (
                        "\n⚠️ Flagged: " + ", ".join(
                            f"`{m['ticker']}` (jaccard={m['jaccard']:.2f})"
                            for m in snap.get("suspicious_matches", [])[:5]
                        ) if snap.get("suspicious_matches") else ""
                    )
                ),
                "inline": False,
            },
        ]

        # Top opportunities
        top_opps = snap.get("top_opportunities", [])
        if top_opps:
            lines = []
            for i, o in enumerate(top_opps[:3], 1):
                ev_str = f" EV={o['net_ev']:+.1f}¢" if o.get("net_ev") is not None else ""
                lines.append(
                    f"{i}. `{o['ticker']}` {(o.get('side') or '').upper()} — "
                    f"conf={o.get('confidence',0):.0f}%{ev_str}\n"
                    f"   _{(o.get('reasoning') or '')[:100]}_"
                )
            fields.append({"name": "🏆 Top Opportunities Today", "value": "\n".join(lines), "inline": False})
        else:
            fields.append({"name": "🏆 Top Opportunities Today", "value": "_Nothing cleared all criteria — cash held._", "inline": False})

        # Closed trades today
        if closed_today:
            lines = []
            for t in closed_today[:8]:
                t_pnl = t.get("pnl") or 0
                icon  = "✅" if t_pnl >= 0 else "❌"
                sign  = "+" if t_pnl >= 0 else ""
                why   = t.get("close_reason", "")
                label = (
                    "resolved"     if why.startswith("resolved")    else
                    "stop-loss"    if why.startswith("stop_loss")   else
                    "take-profit"  if why.startswith("take_profit") else
                    "AI opted out" if why.startswith("ai_reeval")   else "closed"
                )
                lines.append(f"{icon} `{t.get('ticker','')}` {(t.get('side') or '').upper()} **${sign}{t_pnl:.2f}** — {label}")
            fields.append({"name": "📋 Closed Trades Today", "value": "\n".join(lines), "inline": False})

        # Errors
        if err_count:
            recent = snap.get("errors", [])[-3:]
            err_lines = [f"• {msg}" for _, msg in recent]
            fields.append({"name": f"⚠️ Errors ({err_count} total)", "value": "\n".join(err_lines)[:400], "inline": False})

        payload = self._embed(
            title=f"📊 {mode_tag} Daily Report — {date}",
            description=(
                f"End-of-day summary for **{date}**. "
                f"Bot ran for **{snap.get('uptime','?')}** scanning prediction markets 24/7."
            ),
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def trade_review(
        self,
        ticker: str,
        platform: str,
        intended_side: str,
        intended_price: float,
        size_usd: float,
        verdict: str,           # STRONG_BUY / GOOD_TRADE / WRONG_SIDE / BAD_TRADE / PASS
        ai_side: str,           # what side AI recommends
        ai_confidence: float,
        true_prob: float,       # AI's estimated true probability (0-100)
        net_ev: float,
        reasoning: str,
        context_snippet: str,   # first 300 chars of real-world context
        correct_price: float = 0,   # market's actual price for AI's recommended side
    ) -> None:
        """Post a trade advisor review to Discord."""
        verdict_upper = verdict.upper()

        if verdict_upper in ("STRONG_BUY", "GOOD_TRADE"):
            color = 0x00FF00
            icon  = "✅"
            title_tag = "STRONG BUY" if verdict_upper == "STRONG_BUY" else "GOOD TRADE"
            desc = f"✅ **{title_tag}** — go ahead and place this trade."
        elif verdict_upper == "WRONG_SIDE":
            color = 0xFF8C00
            icon  = "⚠️"
            desc  = f"⚠️ Good prediction, **WRONG SIDE** — flip to **{ai_side.upper()}**"
            title_tag = "WRONG SIDE"
        elif verdict_upper == "BAD_TRADE":
            color = 0xFF0000
            icon  = "❌"
            desc  = "❌ **OPT OUT** — negative edge on this trade."
            title_tag = "BAD TRADE"
        else:  # PASS
            color = 0x808080
            icon  = "💤"
            desc  = "💤 **PASS** — no clear edge, sit this one out."
            title_tag = "PASS"

        # Expected profit: (size / price * 100) contracts * net_ev / 100
        contracts_approx = (size_usd / intended_price * 100) if intended_price > 0 else 0
        exp_profit = contracts_approx * net_ev / 100

        fields = [
            {
                "name":   "Your Trade",
                "value":  f"{intended_side.upper()} @ {intended_price:.0f}¢ on {platform.capitalize()}",
                "inline": True,
            },
            {
                "name":   "AI Verdict",
                "value":  f"{icon} {title_tag}",
                "inline": True,
            },
            {
                "name":   "AI True Prob",
                "value":  f"{true_prob:.0f}% (market implies {intended_price:.0f}%)",
                "inline": True,
            },
            {
                "name":   "Net EV",
                "value":  f"{net_ev:.1f}¢ per contract",
                "inline": True,
            },
            {
                "name":   "Confidence",
                "value":  f"{ai_confidence:.0f}%",
                "inline": True,
            },
            {
                "name":   "Expected Profit",
                "value":  f"${exp_profit:.2f}",
                "inline": True,
            },
            {
                "name":   "Real-World Data",
                "value":  (context_snippet[:300] if context_snippet else "_No context available_"),
                "inline": False,
            },
            {
                "name":   "AI Reasoning",
                "value":  (reasoning[:300] if reasoning else "_No reasoning_"),
                "inline": False,
            },
        ]

        if verdict_upper == "WRONG_SIDE" and correct_price > 0:
            fields.append({
                "name":   "✅ Correct Trade",
                "value":  f"BUY {ai_side.upper()} @ {correct_price:.0f}¢",
                "inline": True,
            })

        platform_tag = "🟣 Polymarket" if platform.lower() == "polymarket" else "🟦 Kalshi"
        payload = self._embed(
            title=f"🔍 Trade Advisor — {ticker} [{platform_tag}]",
            description=desc,
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def midnight_daily_summary(
        self,
        date: str,
        stats: Dict,
        paper: bool = True,
        wins: int = 0,
        losses: int = 0,
        today_pnl: float = 0.0,
        all_time_pnl: float = 0.0,
        open_positions: int = 0,
        closed_trades: Optional[List[Dict]] = None,
    ) -> None:
        """
        Post the midnight daily report to Discord.

        Parameters
        ----------
        date            : ISO date string, e.g. "2026-06-01"
        stats           : snapshot dict from DailyStats.snapshot()
        paper           : True if bot is in paper-trading mode
        wins / losses   : today's win/loss count from the DB
        today_pnl       : today's realised PnL in dollars
        all_time_pnl    : all-time cumulative PnL in dollars
        open_positions  : count of positions still open at midnight
        closed_trades   : list of closed position dicts (ticker, side, pnl, close_reason)
        """
        mode_tag  = "📝 Paper (no real money)" if paper else "💰 LIVE"
        color     = 0x00FF00 if today_pnl >= 0 else 0xFF4444
        pnl_sign  = "+" if today_pnl >= 0 else ""
        at_sign   = "+" if all_time_pnl >= 0 else ""

        total_decided = wins + losses
        win_rate = (wins / total_decided * 100) if total_decided > 0 else 0.0

        # ── Section 1: Bot Status ──────────────────────────────────────────
        status_val = (
            f"Running for **{stats.get('uptime', 'unknown')}**\n"
            f"Mode: **{mode_tag}**"
        )

        # ── Section 2: Activity ───────────────────────────────────────────
        activity_val = (
            f"Markets scanned today: **{stats.get('markets_scanned', 0):,}**\n"
            f"AI signals generated: **{stats.get('signals_generated', 0)}** "
            f"(these are candidates before safety checks)\n"
            f"Trades actually placed: **{stats.get('trades_executed', 0)}**\n"
            f"Trades skipped by safety rules: **{stats.get('trades_skipped', 0)}**"
        )

        # ── Section 3: Performance ────────────────────────────────────────
        if total_decided == 0:
            perf_val = "No completed trades yet — the bot is still building its track record."
        else:
            wr_emoji = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"
            perf_val = (
                f"{wr_emoji} Win rate: **{win_rate:.0f}%** — {wins} wins / {losses} losses\n"
                f"Today's profit or loss: **${pnl_sign}{today_pnl:.2f}**\n"
                f"All-time total: **${at_sign}{all_time_pnl:.2f}**"
            )

        # ── Section 4: Polymarket Matching ────────────────────────────────
        suspicious_list = stats.get("suspicious_matches", [])
        poly_val = (
            f"Kalshi↔Polymarket pairs matched: **{stats.get('poly_matches', 0)}**\n"
            f"Suspicious matches flagged for review: **{len(suspicious_list)}**"
        )
        if suspicious_list:
            sample = suspicious_list[:3]
            lines  = [
                f"  ⚠️ `{s['ticker']}` — jaccard={s['jaccard']:.2f} edge={s['net_edge']:+.1f}¢"
                for s in sample
            ]
            poly_val += "\n" + "\n".join(lines)

        # ── Section 5: Top Opportunities ─────────────────────────────────
        top_opps = stats.get("top_opportunities", [])
        if top_opps:
            opp_lines = []
            for i, opp in enumerate(top_opps[:3], 1):
                ev_str = f"{opp['net_ev']:+.1f}¢" if opp.get("net_ev") is not None else "n/a"
                reason_short = (opp.get("reasoning") or "")[:80]
                opp_lines.append(
                    f"**{i}.** `{opp['ticker']}` — BUY {opp.get('side','?').upper()} | "
                    f"score={opp.get('score', 0):.2f} conf={opp.get('confidence', 0):.0f}% "
                    f"EV={ev_str}\n    _{reason_short}_"
                )
            top_val = "\n".join(opp_lines)
        else:
            top_val = "No notable opportunities found today."

        # ── Section 6: Errors ─────────────────────────────────────────────
        error_list = stats.get("errors", [])
        if not error_list:
            error_val = "No errors — clean run ✅"
        else:
            err_count = len(error_list)
            last_three = error_list[-3:]
            err_lines  = [f"• {ts[:19]} — {msg[:80]}" for ts, msg in last_three]
            error_val  = f"{err_count} error(s) today. Last {len(last_three)}:\n" + "\n".join(err_lines)

        # ── Section 7: Closed Trades ──────────────────────────────────────
        def _plain_reason(why: str) -> str:
            if why.startswith("resolved"):
                result = why.split(":")[-1].strip()
                return f"market resolved {result.upper()}" if result else "market resolved"
            if why.startswith("stop_loss"):
                pct = why.split(":")[-1].strip() if ":" in why else ""
                return f"stop-loss hit ({pct})" if pct else "stop-loss hit"
            if why.startswith("take_profit"):
                pct = why.split(":")[-1].strip() if ":" in why else ""
                return f"took profit ({pct})" if pct else "took profit"
            if why.startswith("ai_reeval"):
                return "AI decided to exit early"
            return why or "closed"

        if closed_trades:
            trade_lines = []
            for t in (closed_trades or [])[:10]:
                t_pnl  = t.get("pnl", 0) or 0
                sign   = "+" if t_pnl >= 0 else ""
                icon   = "✅" if t_pnl >= 0 else "❌"
                reason = _plain_reason(t.get("close_reason", ""))
                trade_lines.append(
                    f"{icon} `{t.get('ticker','')}` {(t.get('side') or '').upper()} — "
                    f"**${sign}{t_pnl:.2f}** — {reason}"
                )
            closed_val = "\n".join(trade_lines) or "None"
        else:
            closed_val = "No trades were closed today."

        fields = [
            {"name": "🤖 Bot Status",            "value": status_val,   "inline": False},
            {"name": "📈 Activity",               "value": activity_val, "inline": False},
            {"name": "💰 Performance",            "value": perf_val,     "inline": False},
            {"name": "🔗 Polymarket Matching",    "value": poly_val,     "inline": False},
            {"name": "🏆 Top Opportunities Today","value": top_val,      "inline": False},
            {"name": "⚠️ Errors",                 "value": error_val,    "inline": False},
            {"name": "📋 Closed Trades Today",    "value": closed_val,   "inline": False},
        ]

        payload = self._embed(
            title=f"📊 Daily Report — {date}",
            description=(
                f"Here's what the bot did today. "
                f"{'This is a simulation — no real money was used.' if paper else 'This involved REAL money.'}"
            ),
            color=color,
            fields=fields,
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
