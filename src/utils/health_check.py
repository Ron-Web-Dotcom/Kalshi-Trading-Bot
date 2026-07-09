"""Startup health check — verify all services before bot begins trading."""

import logging
import time
from dataclasses import dataclass
from typing import Dict

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


# ── DB-level health check called after initialize() ────────────────────────

async def run_health_check(db) -> bool:
    """Returns True if all critical checks pass. Logs warnings for failures."""
    passed = 0
    failed = 0

    async def check(name: str, coro):
        nonlocal passed, failed
        try:
            result = await coro
            if result:
                logger.info("  ✅ %s", name)
                passed += 1
            else:
                logger.warning("  ❌ %s — FAILED", name)
                failed += 1
        except Exception as e:
            logger.warning("  ❌ %s — ERROR: %s", name, e)
            failed += 1

    logger.info("=== Bot Health Check ===")

    # DB tables exist
    async def _table(name):
        r = await db.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        )
        return r is not None

    await check("DB: markets table", _table("markets"))
    await check("DB: positions table", _table("positions"))
    await check("DB: trade_logs table", _table("trade_logs"))

    # trade_logs has required columns (resolved_at, result, exit_price added in recent migration)
    async def _col(table, col):
        rows = await db.fetchall(f"PRAGMA table_info({table})")
        return any(r["name"] == col for r in (rows or []))

    await check("trade_logs: resolved_at column", _col("trade_logs", "resolved_at"))
    await check("trade_logs: result column", _col("trade_logs", "result"))
    await check("trade_logs: exit_price column", _col("trade_logs", "exit_price"))
    await check("positions: title column", _col("positions", "title"))
    await check("positions: avg_price column", _col("positions", "avg_price"))

    # Settings
    async def _settings():
        from src.config.settings import settings
        return hasattr(settings, "trading") and settings.trading.min_ai_confidence > 0
    await check("Settings: trading config loaded", _settings())

    # Discord webhook configured
    async def _discord():
        import os
        return bool(os.environ.get("DISCORD_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK"))
    await check("Discord: webhook configured", _discord())

    # Kalshi credentials
    async def _kalshi():
        import os
        return bool(os.environ.get("KALSHI_API_KEY_ID") and os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    await check("Kalshi: credentials present", _kalshi())

    # Evaluation record shape — verify price_cents makes it into record_evaluation output
    async def _eval_shape():
        from src.utils.daily_stats import DailyStats
        ds = DailyStats()
        ds.record_evaluation(
            ticker="TEST", action="BUY", side="yes", confidence=80.0,
            net_ev=5.0, true_prob=0.8, reasoning="test",
            title="Test market", platform="kalshi",
            close_time="2026-12-31T00:00:00Z", yes_ask=45.0,
        )
        ev = ds.all_evaluations[0] if ds.all_evaluations else {}
        return ev.get("price_cents", 0) > 0 and ev.get("yes_ask", 0) > 0
    await check("Eval schema: price_cents stored in evaluations", _eval_shape())

    # Junk filter working
    async def _junk():
        from src.utils.junk_filter import is_junk
        return is_junk("ivan cepeda") and not is_junk("Will Portugal win today?")
    await check("Junk filter: correctly blocks/passes markets", _junk())

    # Win rate SQL points to trade_logs (not positions)
    async def _winrate_sql():
        r = await db.fetchone(
            "SELECT COUNT(*) as n FROM trade_logs WHERE resolved_at IS NOT NULL AND pnl IS NOT NULL"
        )
        return r is not None
    await check("Win rate: trade_logs query works", _winrate_sql())

    logger.info("=== Health Check: %d passed, %d failed ===", passed, failed)
    return failed == 0
