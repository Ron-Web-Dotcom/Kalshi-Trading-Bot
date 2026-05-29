"""Phase 9 — Discord webhook alerts for trades, signals, and errors."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

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
               fields: Optional[list] = None) -> Dict:
        embed: Dict = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if fields:
            embed["fields"] = fields
        return {"embeds": [embed]}

    async def trade_executed(self, ticker: str, action: str, side: str,
                              price: float, contracts: int, size_dollars: float,
                              pnl: Optional[float], ai_confidence: Optional[float],
                              paper: bool = True) -> None:
        if not self.cfg.alert_on_trade:
            return
        color = 0x00FF00 if action == "BUY" else 0xFF4444
        mode_tag = "📝 PAPER" if paper else "💰 LIVE"
        fields = [
            {"name": "Action", "value": f"{action} {side.upper()}", "inline": True},
            {"name": "Price", "value": f"{price:.0f}¢", "inline": True},
            {"name": "Contracts", "value": str(contracts), "inline": True},
            {"name": "Size", "value": f"${size_dollars:.2f}", "inline": True},
        ]
        if pnl is not None:
            fields.append({"name": "PnL", "value": f"${pnl:+.2f}", "inline": True})
        if ai_confidence is not None:
            fields.append({"name": "AI Confidence", "value": f"{ai_confidence:.0f}%", "inline": True})

        payload = self._embed(
            title=f"{mode_tag} Trade — {ticker}",
            description=f"Market: **{ticker}**",
            color=color,
            fields=fields,
        )
        await self._post(payload)

    async def signal_detected(self, ticker: str, signal_type: str,
                               diff_pct: float, edge_cents: float) -> None:
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
