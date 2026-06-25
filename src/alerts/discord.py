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

    @staticmethod
    def _display_ticker(ticker: str, title: str = "") -> str:
        """Return a human-readable label — never expose raw hex condition IDs."""
        if title and not title.startswith("0x"):
            return title[:80]
        if ticker and not ticker.startswith("0x"):
            return ticker[:60]
        # Polymarket hex conditionId — strip entirely, use title or generic label
        return title[:80] if title else "Polymarket Market"

    async def send_message(self, text: str) -> bool:
        """Post a plain-text message as a minimal Discord embed."""
        payload = {"embeds": [{"description": text[:4000], "color": 0x5865F2}]}
        return await self._post(payload)

    async def test_alert(self, mode: str = "PAPER") -> bool:
        """Send a connectivity test message. Returns True if delivered."""
        from src.utils.eastern_time import format_et, et_label
        payload = self._embed(
            title="✅ Kalshi Bot — Connection Test",
            description=(
                f"Discord webhook is working!\n"
                f"Mode: **{mode}**\n"
                f"Time: {format_et()} {et_label()}"
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

    _STARTUP_COOLDOWN_SECS: float = 180.0  # suppress banner if restarted within 3 min
    _STARTUP_TS_FILE: str = "/tmp/kalshi_bot_last_startup.txt"

    async def startup_banner(self, mode: str, balance: Optional[float] = None,
                              poly_enabled: bool = False,
                              health_results: Optional[Dict] = None) -> None:
        """Send bot startup notification — rate-limited to once per 3 minutes."""
        import time, os
        now = time.time()
        try:
            if os.path.exists(self._STARTUP_TS_FILE):
                with open(self._STARTUP_TS_FILE) as _f:
                    last_ts = float(_f.read().strip())
                since_last = now - last_ts
                if since_last < self._STARTUP_COOLDOWN_SECS:
                    logger.info(
                        "Startup banner suppressed (restarted %.0fs ago — cooldown %.0fs)",
                        since_last, self._STARTUP_COOLDOWN_SECS,
                    )
                    return
        except Exception:
            pass
        try:
            with open(self._STARTUP_TS_FILE, "w") as _f:
                _f.write(str(now))
        except Exception:
            pass
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

        # Service health status table
        if health_results:
            lines = []
            for name, result in health_results.items():
                if result.ok:
                    latency = result.latency_ms
                    slow_threshold = 8000
                    slow = latency > slow_threshold
                    icon = "⚠️" if slow else "✅"
                    note = " (slow)" if slow else ""
                    lines.append(f"{icon} {name:<12} — {latency:.0f}ms{note}")
                else:
                    lines.append(f"❌ {name:<12} — FAILED ({result.message[:60]})")
            if lines:
                fields.append({
                    "name": "🔧 Service Health",
                    "value": "```\n" + "\n".join(lines) + "\n```",
                    "inline": False,
                })

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
        """✅ ALERT 2 of 3 — BID PLACED. Trade is in."""
        if not self.cfg.alert_on_trade:
            return
        mode_tag   = "📝 PAPER" if paper else "💰 LIVE"
        max_payout = contracts * (100 - price) / 100
        ev_s       = f" | EV {net_ev:+.1f}¢" if net_ev is not None else ""
        exp_s      = f" | Expected profit **${exp_profit:.2f}**" if exp_profit else ""
        display    = self._display_ticker(ticker, market_title)

        body = (
            f"**{display}**\n"
            f"Side: **{side.upper()}** @ **{price:.0f}¢** | {contracts} contracts | Capital: **${size_dollars:.2f}**\n"
            f"Max payout: **${max_payout:.2f}**{ev_s}{exp_s}\n"
            f"Confidence: **{ai_confidence:.0f}%**\n"
        )
        if reasoning:
            body += f"\n_{reasoning[:200]}_"

        payload = self._embed(
            title=f"✅ BID PLACED  [{mode_tag}]",
            description=body,
            color=0x00C853,
        )
        await self._post(payload)

    async def bot_alert(self, picks: List[Dict], mode: str = "PAPER") -> None:
        """
        👀 BOT ALERT — one big update every 10 min showing:
          - Active bids already placed (⚡ IN BET)
          - New picks being watched (👀 WATCHING)
        Only fires when content changes. Never sends 10 separate messages.
        """
        if not picks:
            return

        mode_tag = "📝 PAPER" if mode == "PAPER" else "💰 LIVE"
        now_utc  = datetime.now(timezone.utc)

        def _hrs_left(p: Dict):
            ct = p.get("close_time", "")
            if not ct:
                return None
            try:
                cd = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                if cd.tzinfo is None:
                    cd = cd.replace(tzinfo=timezone.utc)
                return (cd - now_utc).total_seconds() / 3600
            except Exception:
                return None

        def _timing_tag(hrs) -> str:
            if hrs is None or hrs < 0:
                return " ⏰ resolving now"
            if hrs <= 3:
                return f" 🔴 LIVE — {hrs:.0f}h left"
            if hrs <= 24:
                return " 🟡 TODAY"
            return " 🌅 TOMORROW"

        def _pick_line(p: Dict) -> str:
            plat   = "🟣" if p.get("platform") == "polymarket" else "🟦"
            title  = self._display_ticker(p.get("ticker", ""), p.get("title", "") or "")[:52]
            side   = (p.get("side") or "YES").upper()
            price  = float(p.get("price_cents") or p.get("yes_ask") or 0)
            conf   = float(p.get("confidence") or 0)
            ev     = p.get("net_ev")
            ev_s   = f" EV {ev:+.1f}¢" if ev is not None else ""
            reason = (p.get("reasoning") or "")[:100]
            timing = _timing_tag(_hrs_left(p))
            return (
                f"{plat} **{title}**{timing}\n"
                f"→ **{side}** @ **{price:.0f}¢** | **{conf:.0f}%**{ev_s}\n"
                f"_{reason}_"
            )

        # Only show picks resolving today or tomorrow — drop anything beyond 48h
        in_bet   = [p for p in picks if p.get("_in_bet") or p.get("is_live") and p.get("contracts")]
        watching_all = [p for p in picks if p not in in_bet]
        watching = [p for p in watching_all if (lambda h: h is None or h <= 48)(_hrs_left(p))]

        sections = []

        if in_bet:
            section = "**⚡ BIDS ACTIVE RIGHT NOW**\n" + "\n\n".join(_pick_line(p) for p in in_bet[:5])
            sections.append(section)

        if watching:
            today_picks = [p for p in watching if (lambda h: h is not None and h <= 24)(_hrs_left(p))]
            week_picks  = [p for p in watching if p not in today_picks]

            if today_picks:
                sections.append(
                    "**🟡 WATCHING — TODAY**\n"
                    + "\n\n".join(_pick_line(p) for p in today_picks[:4])
                )
            if week_picks:
                sections.append(
                    "**🌅 WATCHING — TOMORROW**\n"
                    + "\n\n".join(_pick_line(p) for p in week_picks[:3])
                )

        if not sections:
            return  # nothing worth alerting on

        n_shown = len(in_bet) + len(watching)
        color   = 0xFF4400 if in_bet else 0xFFAA00

        payload = self._embed(
            title=f"🚨 BOT ALERT — {n_shown} Pick{'s' if n_shown > 1 else ''}  [{mode_tag}]",
            description="\n\n".join(sections),
            color=color,
        )
        await self._post(payload)

    async def bot_alert_result(self, pick: Dict, outcome: str, mode: str = "PAPER") -> None:
        """
        🚪 ALERT 3 of 3 — OPT OUT / RESULT.

        outcome:
          "profit" → 🟢 WE GOT THE BAG
          "loss"   → 🔴 WE LOST BUT WE KEEP ON MOVING
          "exit"   → 🚪 HAD TO OPT OUT — here's why
        """
        outcome_map = {
            "profit": (0x00C853, "🟢 WE GOT THE BAG"),
            "loss":   (0xFF1744, "🔴 WE LOST BUT WE KEEP ON MOVING"),
            "exit":   (0xFFD600, "🚪 HAD TO OPT OUT"),
        }
        color, headline = outcome_map.get(outcome, (0x888888, "⚪ CLOSED"))
        mode_tag = "📝 PAPER" if mode == "PAPER" else "💰 LIVE"

        display  = self._display_ticker(pick.get("ticker", ""), pick.get("title", "") or "")[:80]
        side     = (pick.get("side") or "YES").upper()
        entry    = float(pick.get("price_cents") or pick.get("yes_ask") or 0)
        exit_p   = pick.get("exit_price")
        pnl      = pick.get("pnl")
        reason   = (pick.get("result_reason") or pick.get("reasoning") or "")[:200]

        body  = f"**{display}**\n"
        body += f"Side: **{side}** | Entry: **{entry:.0f}¢**"
        if exit_p:
            body += f" → Exit: **{exit_p:.0f}¢**"
        body += "\n"
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            body += f"Result: **${sign}{pnl:.2f}**\n"
        if outcome == "exit":
            body += "Bot cut the position before resolution — conditions changed.\n"
        if reason:
            body += f"\n_{reason}_"

        payload = self._embed(
            title=f"{headline}  [{mode_tag}]",
            description=body,
            color=color,
        )
        await self._post(payload)

    async def live_results_summary(
        self,
        wins:   List[Dict],
        losses: List[Dict],
        exits:  List[Dict],
        mode:   str = "PAPER",
    ) -> None:
        """
        ONE consolidated result message covering all wins + losses + opt-outs.
        Only fires when at least one position closed since last check.
        """
        mode_tag   = "📝 PAPER" if mode == "PAPER" else "💰 LIVE"
        total_pnl  = sum(x.get("pnl", 0) for x in wins + losses + exits)
        pnl_sign   = "+" if total_pnl >= 0 else ""
        total      = len(wins) + len(losses) + len(exits)
        lines      = []

        def _item_line(item: Dict, icon: str) -> str:
            title  = (item.get("title") or "?")[:50]
            side   = (item.get("side") or "YES").upper()
            entry  = item.get("entry", 0)
            pnl    = item.get("pnl", 0)
            reason = (item.get("reason") or "")[:60]
            pnl_s  = f"**${pnl:+.2f}**"
            return f"{icon} **{title}** {side} @ {entry:.0f}¢ → {pnl_s}  _{reason}_"

        for w in wins:
            lines.append(_item_line(w, "🟢"))
        for e in exits:
            lines.append(_item_line(e, "🚪"))
        for l in losses:
            lines.append(_item_line(l, "🔴"))

        color = 0x00C853 if total_pnl > 0 else 0xFF1744 if total_pnl < 0 else 0x888888
        payload = self._embed(
            title=f"📊 LIVE BID RESULTS — {total} closed  [{mode_tag}]",
            description=(
                f"**Net: ${pnl_sign}{total_pnl:.2f}** across {total} position{'s' if total > 1 else ''} "
                f"({len(wins)} win{'s' if len(wins)!=1 else ''}, "
                f"{len(losses)} loss{'es' if len(losses)!=1 else ''}, "
                f"{len(exits)} opt-out{'s' if len(exits)!=1 else ''})\n\n"
                + "\n".join(lines)
            ),
            color=color,
        )
        await self._post(payload)

    async def live_trades_alert(self, trades: List[Dict], mode: str = "PAPER") -> None:
        """
        Single Discord embed announcing live in-play trades being entered right now.
        Each trade dict: {title, platform, side, price_cents, confidence, net_ev,
                          size_usd, contracts, reasoning, hours_to_close}
        """
        if not trades:
            return

        mode_tag = "📝 PAPER" if mode == "PAPER" else "💰 LIVE"
        lines = []
        for i, t in enumerate(trades, 1):
            platform_icon = "🟦" if t.get("platform") == "kalshi" else "🟣"
            title    = self._display_ticker(t.get("ticker", ""), t.get("title", ""))
            side     = (t.get("side") or "yes").upper()
            price    = t.get("price_cents", 0)
            conf     = t.get("confidence", 0)
            ev       = t.get("net_ev") or 0
            size     = t.get("size_usd", 0)
            hours    = t.get("hours_to_close", 0)
            lines.append(
                f"**{i}. {platform_icon} {title}**\n"
                f"   BUY {side} @ {price:.0f}¢ — ${size:.2f} — **{conf:.0f}% conf** — EV {ev:+.1f}¢ — ⏱ {hours:.1f}h left"
            )

        fields = []
        for i, t in enumerate(trades, 1):
            reasoning = (t.get("reasoning") or "")[:200]
            if reasoning:
                title_short = self._display_ticker(t.get("ticker", ""), t.get("title", ""))[:40]
                fields.append({
                    "name": f"#{i} Reasoning — {title_short}",
                    "value": reasoning,
                    "inline": False,
                })

        payload = {
            "embeds": [{
                "title": f"⚡ LIVE TRADES — Entering {len(trades)} position{'s' if len(trades) > 1 else ''} now  [{mode_tag}]",
                "description": "\n\n".join(lines),
                "color": 0xFF8C00,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "footer": {"text": "In-play markets resolving soon — high time sensitivity"},
            }]
        }
        await self._post(payload)

    async def arb_signal(self, ticker: str, signal_type: str,
                          gross_edge: float, net_edge: float,
                          side: str = "", kalshi_price: float = 0,
                          poly_price: float = 0) -> None:
        """Alert for arbitrage signal detected (only if ALERT_ON_SIGNAL=true)."""
        if not self.cfg.alert_on_signal:
            return
        label = self._display_ticker(ticker)
        if signal_type == "internal_arb":
            desc = (
                f"**Internal arb** on _{label}_\n"
                f"YES + NO = {kalshi_price + poly_price:.0f}¢ (should be 100¢)\n"
                f"Gross edge: **{gross_edge:.1f}¢** | Net after fees: **{net_edge:.1f}¢**"
            )
        else:
            desc = (
                f"**Cross-market arb** on _{label}_\n"
                f"Kalshi={kalshi_price:.0f}¢  Poly={poly_price:.0f}¢\n"
                f"Buy **{side.upper()}** on Kalshi | Net edge: **{net_edge:.1f}¢**"
            )
        payload = self._embed(
            title=f"📡 Arb Signal — {label}",
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
            title=f"📡 Signal: {self._display_ticker(ticker)}",
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
            {"name": "Question",  "value": self._display_ticker(ticker, market_title)[:80], "inline": False},
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

        display = self._display_ticker(ticker, market_title)
        payload = self._embed(
            title=f"{trigger_emoji} {mode_tag} Position Closed — {'Profit' if pnl >= 0 else 'Loss'} ${pnl_sign}{abs(pnl):.2f}",
            description=f"_{display}_ · **{side.upper()}** · {contracts} contracts",
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

        title_line = self._display_ticker(ticker, market_title)[:100]
        poly_check = ""
        if poly_yes is not None and poly_no is not None:
            poly_check = (
                f"\nPolymarket agrees: YES {poly_yes:.0f}¢ / NO {poly_no:.0f}¢ — "
                f"cross-platform confirmation of the edge."
            )

        fields = [
            {"name": "❓ Question",          "value": title_line,               "inline": False},
            {"name": "🎲 Bet",               "value": f"**BUY {side.upper()}**","inline": True},
            {"name": "💲 Price",             "value": f"{price_cents:.0f}¢",    "inline": True},
            {"name": "🎯 Confidence",        "value": f"{confidence:.0f}%",     "inline": True},
            {"name": "📈 Expected Profit",   "value": profit_str,               "inline": True},
            {"name": "⚖️ Edge per contract", "value": ev_str,                   "inline": True},
            {"name": "🏦 Platform",          "value": platform_tag,             "inline": True},
        ]
        if reasoning:
            fields.append({
                "name":  "🤖 Why the AI is placing this trade",
                "value": reasoning[:400],
                "inline": False,
            })

        payload = self._embed(
            title=f"🎯 {mode_tag} Trade Placed — BUY {side.upper()} on {title_line}",
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
        if not self.cfg.alert_on_trade and not self.cfg.alert_on_signal:
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

    async def near_miss(self, *args, **kwargs) -> None:
        """Disabled — individual near-miss alerts replaced by hourly near_miss_digest."""
        return

    # Timestamp of last near_miss_digest send — only fire again when genuinely new misses exist
    _last_near_miss_digest_at: str = ""

    async def live_miss_digest(self, paper: bool = True) -> None:
        """
        Hourly live-miss digest — shows ONLY misses from the LIVE SCAN
        that resolved in the past hour and where the bot's prediction was CORRECT.

        Rules:
          - Only live-scan misses (events happening right now, not regular markets)
          - Only resolved in the last 60 minutes
          - Never repeats last hour's tickers — always fresh, rotating each hour
          - Silent if nothing new resolved correctly this hour
        """
        from src.utils.live_miss_tracker import live_miss_tracker
        from src.utils.eastern_time import format_et, et_label

        misses = live_miss_tracker.new_confirmed_misses(window_hours=1.0, scan_type="live")
        if not misses:
            return

        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        et_time  = format_et(fmt="%I:%M %p") + f" {et_label()}"
        lines    = []
        total_left = 0.0

        for i, m in enumerate(misses[:6], 1):
            plat    = "🟣" if m.get("platform") == "polymarket" else "🟦"
            title   = self._display_ticker(m.get("ticker", ""), m.get("title", "") or "")[:60]
            side    = (m.get("side") or "yes").upper()
            conf    = m.get("confidence", 0)
            price   = m.get("yes_ask", 0) if side == "YES" else m.get("no_ask", 0)
            skip_r  = (m.get("skip_reason") or "below threshold")[:70]
            reason  = (m.get("reasoning") or "")[:120]
            pnl     = m.get("potential_pnl") or 0
            total_left += max(pnl, 0)

            pnl_str = f" **+${pnl:.2f}** on $10" if pnl > 0 else ""
            lines.append(
                f"**#{i} {plat} {title}**\n"
                f"Would have bought **{side}** @ **{price:.0f}¢** | **{conf:.0f}% conf**{pnl_str}\n"
                f"Skipped because: _{skip_r}_\n"
                f"_{reason}_"
            )

        summary = (
            f"**{len(misses)} correct prediction{'s' if len(misses) > 1 else ''} the bot saw but didn't bet "
            f"— could have earned **${total_left:.2f}** this hour**\n"
            f"Only showing live-event misses. Rotates every hour — no repeats.\n\n"
        )

        payload = self._embed(
            title=f"👀 MISSED TRADES — Live Events This Hour  [{mode_tag}]  {et_time}",
            description=summary + "\n\n".join(lines),
            color=0xFF6B00,
        )
        await self._post(payload)
        live_miss_tracker.mark_digest_sent([m["ticker"] for m in misses])

    async def near_miss_digest(self, paper: bool = True) -> None:
        """
        Missed-trades digest — fires ONLY when there are genuinely NEW misses since last send.
        One consolidated message, never repeated for the same misses.
        """
        from src.utils.daily_stats import stats as _ds
        from src.utils.eastern_time import format_et, et_label

        misses = _ds.top_near_misses(n=5)
        if not misses:
            return

        last_sent = self.__class__._last_near_miss_digest_at

        # Only send if at least one miss was recorded AFTER the last digest
        new_misses = [
            nm for nm in misses
            if (nm.get("recorded_at") or "") > last_sent
        ]
        if not new_misses:
            return  # nothing new since last send — stay silent

        # Update the watermark before posting so concurrent calls don't double-send
        now_iso = datetime.now(timezone.utc).isoformat()
        self.__class__._last_near_miss_digest_at = now_iso

        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        et_time  = format_et(fmt="%I:%M %p") + f" {et_label()}"
        new_tickers = {nm.get("ticker", "") for nm in new_misses}
        lines = []

        for i, nm in enumerate(misses, 1):
            ev_str  = f" EV {nm['net_ev']:+.1f}¢" if nm.get("net_ev") is not None else ""
            plat    = "🟣" if nm.get("platform") == "polymarket" else "🟦"
            title   = self._display_ticker(nm.get("ticker", ""), nm.get("title", "") or "")[:65]
            reason  = (nm.get("reasoning") or "no reasoning")[:120]
            conf    = nm.get("confidence", 0)
            side    = (nm.get("side") or "yes").upper()
            skip_r  = (nm.get("skip_reason") or "confidence below threshold")[:60]
            new_tag = " 🆕" if nm.get("ticker", "") in new_tickers else ""
            lines.append(
                f"**#{i}{new_tag} {plat} {title}**\n"
                f"BUY {side} — **{conf:.0f}% conf**{ev_str}\n"
                f"Skipped: _{skip_r}_\n"
                f"_{reason}_"
            )

        description = (
            f"**{len(new_misses)} new miss{'es' if len(new_misses) > 1 else ''}** since last check • {et_time}\n"
            "Won't re-send the same misses. 🆕 = new since last digest.\n\n"
            + "\n\n".join(lines)
        )
        payload = self._embed(
            title=f"🟡 Missed Trades  [{mode_tag}]",
            description=description,
            color=0xFFAA00,
        )
        await self._post(payload)

    async def position_monitor(self, positions: list, paper: bool = True) -> None:
        """Send active position monitor — one message showing all open trades (4x daily)."""
        if not positions:
            return
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        lines = []
        total_pnl = 0.0
        # Deduplicate: same platform+side+contracts+avg_price = same position stored twice
        # (Polymarket ticker can change between API cycles — conditionId vs slug vs id)
        seen_keys: set = set()
        seen_titles: set = set()
        deduped = []
        for p in positions:
            title = (p.get("title") or "").strip().lower()
            dedup_key = (
                p.get("platform", "kalshi"),
                p.get("side", ""),
                p.get("contracts", 0),
                round(float(p.get("avg_price") or 0)),
            )
            if dedup_key in seen_keys:
                continue
            if title and title not in ("", "unknown") and title in seen_titles:
                continue
            seen_keys.add(dedup_key)
            if title:
                seen_titles.add(title)
            deduped.append(p)
        positions = deduped
        for p in positions:
            ticker    = p.get("ticker", "?")
            title     = p.get("title", "") or ""
            label     = self._display_ticker(ticker, title)
            side_raw  = (p.get("side") or "yes").lower()
            side      = side_raw.upper()
            contracts = int(p.get("contracts") or 0)
            avg_price = float(p.get("avg_price") or 0)
            cur_price = float(p.get("current_price") or avg_price)
            size_usd  = float(p.get("size_usd") or 0) or round(avg_price * contracts / 100, 2)
            # avg_price and current_price are always in the position's own side price
            # (YES price for YES bets, NO price for NO bets) so formula is the same for both
            pnl = (cur_price - avg_price) * contracts / 100
            total_pnl += pnl
            pnl_sign  = "+" if pnl >= 0 else ""
            pct       = ((cur_price - avg_price) / avg_price * 100) if avg_price else 0
            pct_sign  = "+" if pct >= 0 else ""
            icon      = "📈" if pnl >= 0 else "📉"
            platform  = "🟣" if p.get("platform") == "polymarket" else "🟦"
            lines.append(
                f"{icon} {platform} **{label}** | {side} | {contracts} contracts @ {avg_price:.0f}¢\n"
                f"   Entry: **{avg_price:.0f}¢** → Now: **{cur_price:.0f}¢** "
                f"({pct_sign}{pct:.1f}%) | Capital: **${size_usd:.2f}** | PnL: **${pnl_sign}{pnl:.2f}**"
            )
        total_sign = "+" if total_pnl >= 0 else ""
        payload = self._embed(
            title=f"📊 {mode_tag} Active Position Monitor — {len(deduped)} Open Trade(s)",
            description=(
                "\n\n".join(lines)
                + f"\n\n**Total Unrealised PnL: ${total_sign}{total_pnl:.2f}**"
            ),
            color=0x00CC88 if total_pnl >= 0 else 0xFF4444,
        )
        await self._post(payload)

    async def ai_reeval_hold(self, ticker: str, side: str, pct_change: float,
                              reasoning: str, paper: bool = True) -> None:
        """Alert when AI re-evaluates a position and decides to HOLD (optional — only if ALERT_ON_SIGNAL)."""
        if not self.cfg.alert_on_signal:
            return
        color    = 0x5865F2   # Discord blurple — neutral
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        reeval_label = self._display_ticker(ticker)
        payload  = self._embed(
            title=f"🤖 {mode_tag} AI Re-eval: HOLD — {reeval_label}",
            description=(
                f"AI reviewed _{reeval_label}_ ({side.upper()}) and decided to **HOLD**.\n"
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
            {"name": "Next Summary",        "value": "Tomorrow midnight ET",      "inline": True},
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
                    f"{icon} {self._display_ticker(t.get('ticker',''), t.get('title',''))} {t.get('side','').upper()} — "
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
        unrealised_pnl: float = 0.0,
        live_open_positions: int = 0,
        open_by_platform: Optional[Dict] = None,
        paper: bool = True,
        closed_trades: Optional[List[Dict]] = None,
        win_rate: float = 0.0,
        total_wins: int = 0,
        total_losses: int = 0,
        total_pnl: float = 0.0,
        total_closed: int = 0,
        best_pick: Optional[Dict] = None,
        live_slots: int = 0,
        live_slots_max: int = 3,
        all_evaluations: Optional[List[Dict]] = None,
        live_scan_markets: Optional[List[Dict]] = None,
        regular_scan_top: Optional[List[Dict]] = None,
    ) -> None:
        """Hourly heartbeat — clean stats, watching section, best pick."""
        from src.utils.eastern_time import format_et, et_label
        now_utc  = datetime.now(timezone.utc)
        hhmm     = format_et(now_utc, "%I:%M %p") + f" {et_label()}"
        color    = 0x5865F2

        all_evals = all_evaluations or []

        def _timing(ct: str) -> str:
            """Classify a market by how soon it expires."""
            if not ct:
                return "📅 Long-term"
            try:
                close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=timezone.utc)
                hours = (close_dt - now_utc).total_seconds() / 3600
                if hours < 0:
                    return "⏰ Resolving now"
                if hours <= 3:
                    return "🔥 Ends <3h"
                if hours <= 24:
                    return "⏳ Ends today"
                if hours <= 72:
                    return "📆 Ends this week"
                if hours <= 720:
                    return "🗓 Ends this month"
                return "📅 Long-term"
            except Exception:
                return "📅 Long-term"

        # ── Can I Trust This Bot? ─────────────────────────────────────────────
        if total_closed > 0:
            wr_emoji     = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"
            all_pnl_sign = "+" if total_pnl >= 0 else ""
            record_str   = (
                f"**{win_rate:.0f}% win rate** — {total_wins}W / {total_losses}L / {total_closed} closed\n"
                f"All-time PnL: **${all_pnl_sign}{total_pnl:.2f}**"
            )
        elif all_evals:
            wr_emoji   = "🤖"
            # Prefer BUY signals, fall back to highest-confidence HOLDs
            buys  = [e for e in all_evals if e.get("action") == "BUY"]
            show  = (buys or all_evals)[:3]
            lines = []
            for e in show:
                plat   = "🟣" if e.get("platform") == "polymarket" else "🟦"
                ttl    = self._display_ticker(e.get("ticker", ""), e.get("title", "") or "")[:48]
                action = e.get("action", "HOLD")
                conf   = e.get("confidence", 0)
                ev     = e.get("net_ev")
                ev_s   = f" EV {ev:+.1f}¢" if ev is not None else ""
                a_icon = "🟢 BUY" if action == "BUY" else "⏸ HOLD"
                lines.append(f"{plat} **{ttl}**\n→ {a_icon} {conf:.0f}% conf{ev_s}")
            record_str = (
                "No settled bets yet — track record builds when first trade closes.\n\n"
                "**What the bot evaluated this cycle:**\n\n" + "\n\n".join(lines)
            )
        else:
            wr_emoji   = "🆕"
            record_str = "Just started — scanning now. First evaluation will appear here shortly."

        # ── Scan Results — live events + regular top picks ────────────────────
        eval_by_ticker: Dict[str, Dict] = {}
        for e in all_evals:
            t = e.get("ticker", "")
            if t and t not in eval_by_ticker:
                eval_by_ticker[t] = e

        def _market_line(m: Dict, badge: str = "") -> str:
            plat   = m.get("platform", "kalshi")
            icon   = "🟣" if plat == "polymarket" else "🟦"
            title  = self._display_ticker(m.get("ticker", ""), m.get("title", "") or "")[:55]
            yes    = float(m.get("yes_ask", 0) or 0)
            no     = float(m.get("no_ask", 0) or (100 - yes if yes else 0))
            timing = _timing(m.get("close_time", ""))
            # Only show BUY intent for today's events — future events are WATCHING
            try:
                ct = m.get("close_time", "")
                _cd = datetime.fromisoformat(str(ct).replace("Z", "+00:00")) if ct else None
                if _cd and _cd.tzinfo is None:
                    _cd = _cd.replace(tzinfo=timezone.utc)
                _hrs = (_cd - now_utc).total_seconds() / 3600 if _cd else 999
            except Exception:
                _hrs = 999
            is_today = _hrs <= 24
            ev     = eval_by_ticker.get(m.get("ticker", ""))
            if ev and is_today:
                action = ev.get("action", "HOLD")
                conf   = ev.get("confidence", 0)
                ev_val = ev.get("net_ev")
                ev_s   = f" EV {ev_val:+.1f}¢" if ev_val is not None else ""
                a_icon = "🟢 BUY TODAY" if action == "BUY" else "⏸ HOLD"
                ai_str = f"\n🤖 {a_icon} {conf:.0f}% conf{ev_s}"
            elif ev and not is_today:
                conf   = ev.get("confidence", 0)
                ai_str = f"\n👀 WATCHING — {conf:.0f}% conf (bid placed on game day)"
            else:
                ai_str = "\n👀 WATCHING — not yet evaluated"
            return f"{icon}{badge} **{title}** — {timing}\nYES {yes:.0f}¢ | NO {no:.0f}¢{ai_str}"

        # Live scan section
        live_mkts = live_scan_markets or []
        if live_mkts:
            live_lines = [_market_line(m, " ⚡") for m in live_mkts[:4]]
            live_section = "\n\n".join(live_lines)
        else:
            live_section = "_No live events confirmed this scan_"

        # Regular scan top picks
        reg_mkts = regular_scan_top or top_candidates or []
        reg_lines = []
        seen_reg = set()
        for m in reg_mkts[:6]:
            t = m.get("ticker", "")
            if t in seen_reg:
                continue
            seen_reg.add(t)
            reg_lines.append(_market_line(m))
            if len(reg_lines) >= 4:
                break
        regular_section = "\n\n".join(reg_lines) or "_Scanning... no candidates yet_"

        # ── Open Positions — plain-English PnL ───────────────────────────────
        locked_s = "+" if paper_pnl >= 0 else ""
        paper_s  = "+" if unrealised_pnl >= 0 else ""
        p_emoji  = "💰" if paper_pnl >= 0 else "💸"
        u_emoji  = "📈" if unrealised_pnl >= 0 else "📉"

        if live_slots >= live_slots_max:
            slots_str = f"⚡ **{live_slots}/{live_slots_max}** in-play slots filled"
        elif live_slots > 0:
            slots_str = f"⚡ **{live_slots}/{live_slots_max}** in-play slots active — hunting for more"
        else:
            slots_str = f"⚡ **0/{live_slots_max}** in-play — no live events right now"

        regular_open = max(0, open_positions - live_open_positions)
        plat = open_by_platform or {}
        kal_n  = plat.get("kalshi", 0) + plat.get(None, 0)
        poly_n = plat.get("polymarket", 0)

        if open_positions == 0:
            pos_line = "**0** open bets right now"
        else:
            plat_parts = []
            if kal_n:
                plat_parts.append(f"🟦 **{kal_n}** Kalshi")
            if poly_n:
                plat_parts.append(f"🟣 **{poly_n}** Polymarket")
            plat_str = " + ".join(plat_parts) if plat_parts else f"**{open_positions}**"

            if live_open_positions > 0:
                pos_line = (
                    f"**{open_positions}** open bets ({plat_str}) — "
                    f"⚡ **{live_open_positions}** live in-play + "
                    f"🎯 **{regular_open}** regular"
                )
            else:
                pos_line = f"**{open_positions}** open bets ({plat_str}) — 🎯 all regular"

        positions_val = (
            f"{pos_line}\n"
            f"{p_emoji} **Locked in:** ${locked_s}{paper_pnl:.2f} ← _money already banked from closed bets_\n"
            f"{u_emoji} **On paper:** ${paper_s}{unrealised_pnl:.2f} ← _what we'd pocket if we cashed out NOW_\n"
            f"{slots_str}"
        )

        fields = [
            {
                "name":   f"{wr_emoji} Can I Trust This Bot? (Track Record)",
                "value":  record_str,
                "inline": False,
            },
            {
                "name":   "📡 Markets Scanned",
                "value":  f"🟦 {kalshi_count} Kalshi + 🟣 {poly_count} Polymarket = **{markets_scanned} total**",
                "inline": False,
            },
            {
                "name":   "💼 Open Positions",
                "value":  positions_val,
                "inline": False,
            },
            {
                "name":   "⚡ Live Events This Scan",
                "value":  live_section,
                "inline": False,
            },
            {
                "name":   "🔍 Regular Scan — Top Picks",
                "value":  regular_section,
                "inline": False,
            },
            {
                "name":   "⏱ Next Scan",
                "value":  "in ~60s — running 24/7",
                "inline": False,
            },
        ]

        # Today's closed trade results
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
                title = self._display_ticker(t.get("ticker", ""), t.get("title", "") or "")
                lines.append(f"{icon} **{title}** {(t.get('side') or '').upper()} **${sign}{t_pnl:.2f}** — {label}")
            fields.insert(-1, {
                "name":   "📋 Today's Trade Results",
                "value":  "\n".join(lines),
                "inline": False,
            })

        # Best Pick of the Day — one Kalshi slot + one Polymarket slot
        if best_pick or all_evals:
            from src.utils.daily_stats import stats as _ds
            picks = _ds.best_pick_by_platform()
            pick_lines = []
            for plat_key, p_icon, p_label in [("kalshi", "🟦", "Kalshi"), ("polymarket", "🟣", "Polymarket")]:
                p = picks.get(plat_key)
                if not p:
                    pick_lines.append(f"{p_icon} **{p_label}** — _No evaluation yet this cycle_")
                    continue
                title  = self._display_ticker(p.get("ticker", ""), p.get("title", "") or "")
                side   = (p.get("side") or "YES").upper()
                conf   = p.get("confidence", 0)
                ev     = p.get("net_ev")
                ev_str = f" | EV **{ev:+.1f}¢**" if ev is not None else ""
                action = p.get("action", "HOLD")
                act_s  = "🟢 Bot would BUY" if action == "BUY" else "⏸ Bot is watching (HOLD)"
                reason = (p.get("reasoning") or "")[:120]
                pick_lines.append(
                    f"{p_icon} **{p_label}** — {title}\n"
                    f"{act_s} | **{side}** {conf:.0f}% conf{ev_str}\n"
                    f"_{reason}_"
                )
            fields.insert(-1, {
                "name":  "🧠 Best Pick of the Day",
                "value": "\n\n".join(pick_lines),
                "inline": False,
            })

        payload = self._embed(
            title=f"🔍 Hourly Scan Report — {hhmm}",
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
        unrealised_pnl: float = 0.0,
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
                    f"Today's realised PnL: **${pnl_sign}{today_pnl:.2f}**\n"
                    f"Unrealised PnL (open): **${'+'if unrealised_pnl>=0 else ''}{unrealised_pnl:.2f}**\n"
                    f"All-time realised PnL: **${all_sign}{alltime_pnl:.2f}**\n"
                    f"{wr_emoji} Win rate: **{win_rate:.0f}%** ({wins}W / {losses}L / {total_closed} closed)\n"
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

    async def daytime_summary(
        self,
        period: str,               # "Midnight" / "Morning" / "Afternoon" / "Evening"
        open_positions: list,
        new_positions: list,       # opened since last summary
        today_pnl: float,
        kalshi_count: int,
        poly_count: int,
        win_rate: float,
        total_wins: int,
        total_losses: int,
        total_closed: int,
        paper: bool = True,
        closed_since_last: Optional[List[Dict]] = None,   # settled since last check-in
        best_buys: Optional[List[Dict]] = None,           # top AI BUY picks this cycle
        alltime_pnl: float = 0.0,
        live_positions: Optional[List[Dict]] = None,      # in-play live slots
    ) -> None:
        """Scheduled digest at 12am/6am/12pm/6pm ET — narrative of what the bot did."""
        from src.utils.eastern_time import format_et, et_label
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        icons    = {"Midnight": "🌙", "Morning": "🌅", "Afternoon": "🌇", "Evening": "🌆"}
        p_icon   = icons.get(period, "🕐")
        now_str  = format_et(fmt="%I:%M %p") + f" {et_label()}"
        closed   = closed_since_last or []
        buys     = best_buys or []
        live_pos = live_positions or []

        # ── Deduplicate open positions ────────────────────────────────────────
        _seen_k: set = set()
        _seen_t: set = set()
        deduped_open = []
        for p in (open_positions or []):
            _t = (p.get("title") or "").strip().lower()
            _k = (p.get("platform",""), p.get("side",""), p.get("contracts",0),
                  round(float(p.get("avg_price") or 0)))
            if _k in _seen_k or (_t and _t not in ("","unknown") and _t in _seen_t):
                continue
            _seen_k.add(_k)
            if _t:
                _seen_t.add(_t)
            deduped_open.append(p)

        # ── Determine the headline story ──────────────────────────────────────
        wins_here   = [c for c in closed if (c.get("pnl") or 0) > 0]
        losses_here = [c for c in closed if (c.get("pnl") or 0) < 0]
        has_live    = bool(live_pos)
        has_new     = bool(new_positions)
        top_buy     = next((b for b in buys if b.get("action") == "BUY"), None)

        if has_live and has_new:
            headline = f"⚡🎯 Bot placed a LIVE in-play bet AND a new prediction — it's been busy!"
            color    = 0xFF4400
        elif has_live:
            headline = f"⚡ OH SNAP — Bot entered a LIVE in-play market! Game on! 🎮"
            color    = 0xFF4400
        elif wins_here and has_new:
            total_won = sum(c.get("pnl", 0) or 0 for c in wins_here)
            headline  = f"🏆 Bot banked ${total_won:+.2f} AND placed {len(new_positions)} new bet(s) — let's GO!"
            color     = 0x00FF7F
        elif wins_here:
            total_won = sum(c.get("pnl", 0) or 0 for c in wins_here)
            headline  = f"💰 YES! Bot won — **${total_won:+.2f}** profit locked in. Damn that's good."
            color     = 0x00FF7F
        elif has_new:
            headline = f"🎯 Bot placed **{len(new_positions)}** new prediction bet(s) — confidence was there!"
            color    = 0x00BFFF
        elif top_buy:
            ttl = self._display_ticker(top_buy.get("ticker",""), top_buy.get("title","") or "")[:40]
            headline = f"🧠 Bot spotted a strong edge on **{ttl}** — watching for right entry point"
            color    = 0x5865F2
        elif losses_here:
            total_lost = sum(c.get("pnl", 0) or 0 for c in losses_here)
            headline   = f"📉 Tough one — ${total_lost:.2f} loss this period. Bot's already hunting the next edge."
            color      = 0xFF4444
        else:
            scanned = kalshi_count + poly_count
            headline = f"🔍 Bot analyzed **{scanned:,}** markets — holding cash, waiting for real edge"
            color    = 0x808080

        fields = []

        # ── What the bot did since last check-in ─────────────────────────────
        activity_lines = []

        # New bets placed
        for p in (new_positions or [])[:6]:
            plat  = "🟣" if p.get("platform") == "polymarket" else "🟦"
            side  = (p.get("side") or "yes").upper()
            price = float(p.get("avg_price") or 0)
            size  = float(p.get("size_usd") or 0) or round(price * int(p.get("contracts") or 0) / 100, 2)
            label = self._display_ticker(p.get("ticker","?"), p.get("title","") or "")[:50]
            is_live_bet = p.get("is_live") or p.get("_momentum")
            tag   = " ⚡ LIVE BET" if is_live_bet else ""
            activity_lines.append(
                f"🎯 **Placed:**{tag} {plat} **{label}**\n"
                f"   → BUY **{side}** @ **{price:.0f}¢** | ${size:.2f} in"
            )

        # Bets that settled (won or lost)
        for c in closed[:5]:
            pnl   = c.get("pnl") or 0
            sign  = "+" if pnl >= 0 else ""
            icon  = "✅ WON" if pnl >= 0 else "❌ LOST"
            why   = c.get("close_reason", "")
            how   = (
                "prediction resolved ✔" if why.startswith("resolved") else
                "stop-loss hit"         if why.startswith("stop_loss") else
                "take-profit hit 🎉"    if why.startswith("take_profit") else
                "AI opted out"          if why.startswith("ai_reeval") else "closed"
            )
            label = self._display_ticker(c.get("ticker","?"), c.get("title","") or "")[:50]
            plat  = "🟣" if c.get("platform") == "polymarket" else "🟦"
            activity_lines.append(
                f"{icon}: {plat} **{label}**\n"
                f"   → **${sign}{pnl:.2f}** ({how})"
            )

        if not activity_lines:
            activity_lines.append("_No new bets or settlements this period — scanning continued non-stop_")

        fields.append({
            "name":   f"📋 What The Bot Did (since last check-in)",
            "value":  "\n\n".join(activity_lines),
            "inline": False,
        })

        # ── Live in-play bets ─────────────────────────────────────────────────
        if live_pos:
            llines = []
            for lp in live_pos[:3]:
                label    = self._display_ticker(lp.get("ticker","?"), lp.get("title","") or "")[:50]
                side     = (lp.get("side") or "yes").upper()
                entry    = float(lp.get("entry_price") or lp.get("yes_ask") or 0)
                move     = lp.get("move_pct", 0)
                m_icon   = "📈" if move >= 0 else "📉"
                llines.append(
                    f"⚡ **{label}** — **{side}** in-play\n"
                    f"   Entry: {entry:.0f}¢ | Move: {m_icon} {move:+.1f}%"
                )
            fields.append({
                "name":   "⚡ LIVE In-Play Bets (Active Right Now!)",
                "value":  "\n\n".join(llines),
                "inline": False,
            })

        # ── Open positions — what's riding ────────────────────────────────────
        if deduped_open:
            olines = []
            total_capital = 0.0
            total_payout  = 0.0
            total_unreal  = 0.0
            for p in deduped_open[:8]:
                plat      = "🟣" if p.get("platform") == "polymarket" else "🟦"
                side      = (p.get("side") or "yes").upper()
                avg_price = float(p.get("avg_price") or 0)
                cur_price = float(p.get("current_price") or avg_price)
                contracts = int(p.get("contracts") or 0)
                size_usd  = float(p.get("size_usd") or 0) or round(avg_price * contracts / 100, 2)
                # Profit if this bet wins = contracts × (100 − entry_price) / 100
                # regardless of side (you always paid avg_price and win (100-avg_price))
                profit_if_win = contracts * (100 - avg_price) / 100
                total_capital += size_usd
                total_payout  += profit_if_win
                pnl       = (cur_price - avg_price) * contracts / 100
                total_unreal  += pnl
                label = self._display_ticker(p.get("ticker","?"), p.get("title","") or "")[:46]
                olines.append(
                    f"{plat} **{label}**\n"
                    f"   {side} | {contracts}x @ {avg_price:.0f}¢ | in **${size_usd:.2f}** → profit **+${profit_if_win:.2f}**"
                )
            all_s = "+" if alltime_pnl >= 0 else ""
            tu_s  = "+" if total_unreal >= 0 else ""
            summary = (
                f"💰 **All-time banked: ${all_s}{alltime_pnl:.2f}**\n"
                f"📤 Paper money in: **${total_capital:.2f}**\n"
                f"📥 If all win, profit: **+${total_payout:.2f}**\n"
                + (f"📊 Current move: **${tu_s}{total_unreal:.2f}**\n" if total_unreal != 0 else "")
                + "\n"
            )
            fields.append({
                "name":   f"💼 Open Bets ({len(deduped_open)})",
                "value":  summary + "\n".join(olines),
                "inline": False,
            })
        else:
            all_s = "+" if alltime_pnl >= 0 else ""
            fields.append({
                "name":   "💼 Holding",
                "value":  f"💰 **All-time banked: ${all_s}{alltime_pnl:.2f}**\n_No open bets right now — fully in cash_",
                "inline": False,
            })

        # ── Bot's radar — today's BUYs + upcoming watches ───────────────────
        buy_signals_raw = [b for b in buys if b.get("action") == "BUY"]
        seen_tickers: set = set()
        buy_signals = []
        for b in sorted(buy_signals_raw, key=lambda x: x.get("confidence", 0), reverse=True):
            t = b.get("ticker", "")
            if t not in seen_tickers:
                seen_tickers.add(t)
                buy_signals.append(b)

        def _hrs_to_close(b):
            ct = b.get("close_time", "")
            if not ct:
                return 999
            try:
                cd = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                if cd.tzinfo is None:
                    cd = cd.replace(tzinfo=timezone.utc)
                return (cd - datetime.now(timezone.utc)).total_seconds() / 3600
            except Exception:
                return 999

        today_buys   = [b for b in buy_signals if _hrs_to_close(b) <= 24][:3]
        watch_buys   = [b for b in buy_signals if _hrs_to_close(b) > 24][:3]

        if today_buys or watch_buys:
            rlines = []
            for b in today_buys:
                plat   = "🟣" if b.get("platform") == "polymarket" else "🟦"
                label  = self._display_ticker(b.get("ticker",""), b.get("title","") or "")[:48]
                conf   = b.get("confidence", 0)
                ev     = b.get("net_ev")
                ev_s   = f" | EV {ev:+.1f}¢" if ev is not None else ""
                side   = (b.get("side") or "YES").upper()
                reason = (b.get("reasoning") or "")[:90]
                rlines.append(
                    f"🟢 {plat} **{label}**\n"
                    f"   BUY **{side}** TODAY — {conf:.0f}% conf{ev_s}\n"
                    f"   _{reason}_"
                )
            for b in watch_buys:
                plat   = "🟣" if b.get("platform") == "polymarket" else "🟦"
                label  = self._display_ticker(b.get("ticker",""), b.get("title","") or "")[:48]
                conf   = b.get("confidence", 0)
                hrs    = _hrs_to_close(b)
                days   = int(hrs / 24)
                rlines.append(
                    f"👀 {plat} **{label}**\n"
                    f"   WATCHING — {conf:.0f}% conf — bid placed on game day (~{days}d away)"
                )
            fields.append({
                "name":   "🧠 Bot's Best Picks Right Now",
                "value":  "\n\n".join(rlines),
                "inline": False,
            })

        # ── Missed trades (near-misses) ───────────────────────────────────────
        try:
            from src.utils.daily_stats import stats as _ds_nm
            nm_list = _ds_nm.top_near_misses(n=3)
            if nm_list:
                nm_lines = []
                for nm in nm_list:
                    plat_nm = "🟣" if nm.get("platform") == "polymarket" else "🟦"
                    ttl_nm  = self._display_ticker(nm.get("ticker",""), nm.get("title","") or "")[:50]
                    conf_nm = nm.get("confidence", 0)
                    side_nm = (nm.get("side") or "YES").upper()
                    skip_nm = (nm.get("skip_reason") or "below threshold")[:55]
                    nm_lines.append(f"{plat_nm} **{ttl_nm}** — BUY {side_nm} {conf_nm:.0f}% conf\n_{skip_nm}_")
                fields.append({
                    "name":   "👀 Trades Bot Saw But Didn't Take",
                    "value":  "\n\n".join(nm_lines),
                    "inline": False,
                })
        except Exception:
            pass

        # ── Track record ──────────────────────────────────────────────────────
        pnl_s    = "+" if today_pnl >= 0 else ""
        if total_closed == 0:
            wr_emoji = "🆕"
            wr_str   = "No settled bets yet — track record in progress"
        else:
            wr_emoji = "🟢" if win_rate >= 55 else "🟡" if win_rate >= 45 else "🔴"
            wr_str   = f"**{win_rate:.0f}% win rate** — {total_wins}W / {total_losses}L / {total_closed} settled"
        today_line = f"Today's settled PnL: **${pnl_s}{today_pnl:.2f}**\n" if today_pnl != 0 else ""
        fields.append({
            "name":   f"{wr_emoji} Track Record",
            "value":  (
                f"{wr_str}\n"
                f"{today_line}"
                f"Scanning: 🟦 **{kalshi_count}** Kalshi + 🟣 **{poly_count}** Polymarket"
            ),
            "inline": False,
        })

        payload = self._embed(
            title=f"{p_icon} {mode_tag} {period} Check-In — {now_str}",
            description=headline,
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
            title=f"🔍 Trade Advisor — {self._display_ticker(ticker)} [{platform_tag}]",
            description=desc,
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def ai_budget_cap_hit(self, spent: float, cap: float) -> bool:
        """One-time white alert when AI spend hits the hard daily cap."""
        payload = self._embed(
            title="🤖💸 AI BUDGET CAP HIT",
            description=(
                f"Daily AI spend has reached the **${cap:.2f} hard cap**.\n"
                f"All AI calls are paused for the rest of today.\n\n"
                f"**Spent:** ${spent:.2f}  |  **Cap:** ${cap:.2f}\n"
                f"Scanning and tracking continue — trading resumes tomorrow."
            ),
            color=0xFFFFFF,  # white
            fields=[
                {"name": "Action taken", "value": "AI calls suspended for today", "inline": False},
                {"name": "Next reset", "value": "Midnight (auto-resumes)", "inline": True},
            ],
        )
        return await self._post(payload)

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
