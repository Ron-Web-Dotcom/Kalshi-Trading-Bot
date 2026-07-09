"""Append-only audit log for every trade action."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.audit_log")

_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
_AUDIT_FILE = _LOG_DIR / "audit.log"


class AuditLogger:
    """Writes every trade action to both the audit_log DB table and logs/audit.log."""

    async def log(
        self,
        db,
        event_type: str,
        ticker: str = "",
        platform: str = "kalshi",
        side: str = "",
        price_cents: float = 0,
        size_usd: float = 0,
        confidence: float = 0,
        net_ev: Optional[float] = None,
        reason: str = "",
        result: str = "PENDING",
        pnl: Optional[float] = None,
        operator: str = "bot",
    ) -> None:
        """Insert audit row into DB and append to logs/audit.log."""
        now = datetime.now(_ET).isoformat()

        # ── File log ──────────────────────────────────────────────────────────
        ev_str = f"{net_ev:.2f}¢" if net_ev is not None else "n/a"
        line = (
            f"{now} | {event_type} | {ticker} | {side} | {price_cents:.0f}¢ "
            f"| ${size_usd:.2f} | conf={confidence:.0f}% | ev={ev_str} "
            f"| {reason} | {result}\n"
        )
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            with _AUDIT_FILE.open("a") as f:
                f.write(line)
        except Exception as e:
            logger.warning("Audit file write failed: %s", e)

        # ── DB insert ─────────────────────────────────────────────────────────
        if db is None:
            return
        try:
            await db.execute(
                """
                INSERT INTO audit_log
                  (event_type, ticker, platform, side, price_cents, size_usd,
                   confidence, net_ev, reason, result, pnl, operator, logged_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_type, ticker, platform, side, price_cents, size_usd,
                    confidence, net_ev, reason, result, pnl, operator, now,
                ),
            )
        except Exception as e:
            logger.warning("Audit DB insert failed: %s", e)


# Module-level singleton
auditor = AuditLogger()
