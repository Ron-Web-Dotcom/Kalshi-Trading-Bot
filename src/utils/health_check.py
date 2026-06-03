"""Startup health check — verify all services before bot begins trading."""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx

logger = logging.getLogger("trading.health_check")

_SLOW_MS = 8000  # latency above this is flagged as slow


@dataclass
class HealthResult:
    service: str
    ok: bool
    latency_ms: float
    message: str


class HealthChecker:
    """Checks all external services at startup and reports their status."""

    async def _check_kalshi(self) -> HealthResult:
        from src.config.settings import settings
        base_url = settings.kalshi.base_url.rstrip("/")
        # Use exchange status endpoint; fall back to markets list
        url = base_url + "/exchange/status"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                trading_active = data.get("trading_active", True)
                msg = "operational" if trading_active else "exchange not active"
                return HealthResult("Kalshi", True, latency_ms, msg)
            return HealthResult("Kalshi", False, latency_ms, f"HTTP {resp.status_code}")
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            return HealthResult("Kalshi", False, latency_ms, str(e)[:120])

    async def _check_polymarket(self) -> HealthResult:
        url = "https://gamma-api.polymarket.com/markets?limit=1"
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                has_data = bool(data) if isinstance(data, list) else bool(data.get("data"))
                msg = "ok" if has_data else "empty response"
                return HealthResult("Polymarket", True, latency_ms, msg)
            return HealthResult("Polymarket", False, latency_ms, f"HTTP {resp.status_code}")
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            return HealthResult("Polymarket", False, latency_ms, str(e)[:120])

    async def _check_discord(self) -> HealthResult:
        from src.config.settings import settings
        webhook_url = settings.alerts.discord_webhook_url
        if not webhook_url:
            return HealthResult("Discord", False, 0, "DISCORD_WEBHOOK_URL not set")
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    webhook_url,
                    json={"content": "🔧 Health check — bot starting up"},
                )
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code in (200, 204):
                return HealthResult("Discord", True, latency_ms, "ok")
            return HealthResult("Discord", False, latency_ms, f"HTTP {resp.status_code}")
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            return HealthResult("Discord", False, latency_ms, str(e)[:120])

    async def _check_ai(self) -> HealthResult:
        from src.config.settings import settings
        api_key = settings.ai.openai_api_key
        if not api_key:
            return HealthResult("AI", False, 0, "OPENAI_API_KEY not set")
        t0 = time.monotonic()
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=api_key)
            resp = await client.chat.completions.create(
                model=settings.ai.model,
                max_tokens=16,
                messages=[{"role": "user", "content": "Reply OK"}],
            )
            latency_ms = (time.monotonic() - t0) * 1000
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            ok = bool(text)
            msg = "ok" if ok else "empty response"
            return HealthResult("AI", ok, latency_ms, msg)
        except Exception as e:
            latency_ms = (time.monotonic() - t0) * 1000
            return HealthResult("AI", False, latency_ms, str(e)[:120])

    async def run_all(self) -> Dict[str, HealthResult]:
        """
        Run all service checks. Returns dict of name → HealthResult.
        Never raises — degraded mode is better than no bot.
        """
        import asyncio

        checks = await asyncio.gather(
            self._check_kalshi(),
            self._check_polymarket(),
            self._check_discord(),
            self._check_ai(),
            return_exceptions=True,
        )

        names = ["Kalshi", "Polymarket", "Discord", "AI"]
        results: Dict[str, HealthResult] = {}
        for name, result in zip(names, checks):
            if isinstance(result, Exception):
                results[name] = HealthResult(name, False, 0, str(result)[:120])
            else:
                results[name] = result

        # Log summary
        for name, r in results.items():
            slow_note = " (slow)" if r.ok and r.latency_ms > _SLOW_MS else ""
            if r.ok:
                logger.info(
                    "Health check %-12s OK     %.0fms%s",
                    name, r.latency_ms, slow_note,
                )
            else:
                logger.error(
                    "Health check %-12s FAILED — %s",
                    name, r.message,
                )

        return results
