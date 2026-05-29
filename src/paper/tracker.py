"""Paper trading tracker — signal logging and settlement."""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("trading.paper_tracker")


async def log_signal(db, ticker: str, action: str, side: str,
                     price: float, contracts: int = 1,
                     ai_confidence: float = 0, ai_reasoning: str = "",
                     arbitrage_pct: float = 0, signal_source: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat()
    return await db.insert("paper_signals", {
        "ticker": ticker,
        "action": action,
        "side": side,
        "price": price,
        "contracts": contracts,
        "ai_confidence": ai_confidence,
        "ai_reasoning": ai_reasoning[:500] if ai_reasoning else "",
        "arbitrage_pct": arbitrage_pct,
        "signal_source": signal_source,
        "outcome": None,
        "settled": 0,
        "created_at": now,
    })


async def settle_signal(db, signal_id: int, outcome: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE paper_signals SET outcome=?, settled=1, settled_at=? WHERE id=?",
        (outcome, now, signal_id)
    )


async def get_pending_signals(db) -> List[Dict]:
    return await db.fetchall("SELECT * FROM paper_signals WHERE settled=0 ORDER BY created_at DESC")


async def get_all_signals(db, limit: int = 100) -> List[Dict]:
    return await db.fetchall(
        "SELECT * FROM paper_signals ORDER BY created_at DESC LIMIT ?", (limit,)
    )


async def get_stats(db) -> Dict:
    row = await db.fetchone("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN settled=1 AND outcome > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN settled=1 AND outcome <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(COALESCE(outcome, 0)) as total_pnl
        FROM paper_signals
    """)
    if not row:
        return {}
    wins = row["wins"] or 0
    losses = row["losses"] or 0
    total = wins + losses
    row["wins"] = wins
    row["losses"] = losses
    row["win_rate"] = (wins / total * 100) if total > 0 else 0.0
    return row
