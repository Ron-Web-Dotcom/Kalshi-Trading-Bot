"""
Discord command listener — polls a Discord channel for bot commands.

Supported commands (type in your Discord channel):
  !live on      — enable live trading
  !live off     — disable live trading (back to paper)
  !pause        — pause all trading cycles (kill switch)
  !resume       — resume trading
  !status       — bot replies with current mode + open positions
  !close <tick> — close a specific open position

Requires DISCORD_BOT_TOKEN and DISCORD_COMMAND_CHANNEL_ID in .env.
Get a bot token at: https://discord.com/developers/applications
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("trading.discord_commands")

_DISCORD_API = "https://discord.com/api/v10"
_last_message_id: Optional[str] = None


class DiscordCommandListener:
    def __init__(self, db=None):
        from src.config.settings import settings
        import os
        self.token      = os.environ.get("DISCORD_BOT_TOKEN", "")
        self.channel_id = os.environ.get("DISCORD_COMMAND_CHANNEL_ID", "")
        self.db         = db
        self.enabled    = bool(self.token and self.channel_id)

    def _headers(self):
        return {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}

    async def _get_messages(self, after: Optional[str] = None):
        if not self.enabled:
            return []
        params = {"limit": 10}
        if after:
            params["after"] = after
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{_DISCORD_API}/channels/{self.channel_id}/messages",
                    headers=self._headers(),
                    params=params,
                )
                if resp.status_code == 200:
                    return resp.json()
        except Exception as e:
            logger.debug("Discord poll error: %s", e)
        return []

    async def _send(self, content: str):
        if not self.enabled:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{_DISCORD_API}/channels/{self.channel_id}/messages",
                    headers=self._headers(),
                    json={"content": content},
                )
        except Exception as e:
            logger.debug("Discord send error: %s", e)

    async def poll_and_execute(self) -> None:
        """Poll for new commands and execute them."""
        global _last_message_id
        if not self.enabled:
            return

        messages = await self._get_messages(after=_last_message_id)
        if not messages:
            return

        # Discord returns newest first — process oldest first
        for msg in reversed(messages):
            msg_id   = msg.get("id", "")
            content  = (msg.get("content") or "").strip()
            author   = msg.get("author", {})
            is_bot   = author.get("bot", False)

            if msg_id > (_last_message_id or "0"):
                _last_message_id = msg_id

            if is_bot or not content.startswith("!"):
                continue

            logger.info("Discord command received: %s", content)
            await self._handle(content.lower())

    async def _handle(self, cmd: str) -> None:
        from src.config.settings import settings
        from src.utils.kill_switch import engage as ks_engage, clear as ks_clear, is_active as ks_active

        if cmd == "!live on":
            settings.trading.live_trading_enabled = True
            settings.trading.paper_trading_mode   = False
            await self._send("✅ **Live trading ENABLED** — bot will now place real orders. Stay sharp.")
            logger.info("Discord command: live trading ENABLED")

        elif cmd == "!live off":
            settings.trading.live_trading_enabled = False
            settings.trading.paper_trading_mode   = True
            await self._send("📝 **Live trading DISABLED** — switched back to paper mode.")
            logger.info("Discord command: live trading DISABLED")

        elif cmd == "!pause":
            ks_engage("Paused via Discord command")
            await self._send("⏸️ **Bot PAUSED** — all trading cycles halted. Type `!resume` to restart.")

        elif cmd == "!resume":
            ks_clear()
            await self._send("▶️ **Bot RESUMED** — trading cycles restarted.")

        elif cmd == "!status":
            await self._send_status()

        elif cmd.startswith("!close "):
            ticker = cmd.split(" ", 1)[1].strip().upper()
            await self._close_position(ticker)

        elif cmd == "!help":
            await self._send(
                "**Bot Commands:**\n"
                "`!live on` — enable live trading\n"
                "`!live off` — switch to paper mode\n"
                "`!pause` — pause all trading\n"
                "`!resume` — resume trading\n"
                "`!status` — current mode + open positions\n"
                "`!close <TICKER>` — close a specific position\n"
                "`!help` — show this message"
            )

    async def _send_status(self) -> None:
        from src.config.settings import settings
        from src.utils.kill_switch import is_active as ks_active

        mode    = "🔴 LIVE" if settings.trading.live_trading_enabled else "📝 PAPER"
        paused  = "⏸️ PAUSED" if ks_active() else "▶️ RUNNING"

        positions_str = "_No open positions_"
        if self.db:
            try:
                rows = await self.db.fetchall(
                    "SELECT ticker, title, side, contracts, avg_price, pnl "
                    "FROM positions WHERE status='open' ORDER BY opened_at DESC LIMIT 10"
                )
                if rows:
                    lines = []
                    for r in rows:
                        pnl  = r.get("pnl") or 0
                        sign = "+" if pnl >= 0 else ""
                        icon = "📈" if pnl >= 0 else "📉"
                        label = (r.get("title") or r.get("ticker") or "?")[:40]
                        lines.append(
                            f"{icon} **{label}** | {(r.get('side') or '').upper()} "
                            f"× {r.get('contracts',0)} | entry {r.get('avg_price',0):.0f}¢ "
                            f"| PnL **${sign}{pnl:.2f}**"
                        )
                    positions_str = "\n".join(lines)
            except Exception:
                pass

        await self._send(
            f"**Bot Status**\n"
            f"Mode: {mode} | State: {paused}\n\n"
            f"**Open Positions:**\n{positions_str}"
        )

    async def _close_position(self, ticker: str) -> None:
        if not self.db:
            await self._send(f"❌ Cannot close `{ticker}` — no DB connection.")
            return
        try:
            row = await self.db.fetchone(
                "SELECT * FROM positions WHERE ticker=? AND status='open'", (ticker,)
            )
            if not row:
                await self._send(f"❌ No open position found for `{ticker}`.")
                return

            now = datetime.now(timezone.utc).isoformat()
            await self.db.execute(
                "UPDATE positions SET status='closed', close_reason='manual_discord', closed_at=? WHERE ticker=? AND status='open'",
                (now, ticker)
            )
            await self._send(f"✅ Position `{ticker}` closed via Discord command.")
            logger.info("Position %s closed via Discord command", ticker)
        except Exception as e:
            await self._send(f"❌ Error closing `{ticker}`: {e}")
