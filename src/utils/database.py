"""SQLite database manager — async, all schema managed here."""

import aiosqlite
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trading.database")


class DatabaseManager:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            from src.config.settings import settings
            db_path = settings.database.path
        self.db_path = db_path
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if self._initialized:
                return
            async with aiosqlite.connect(self.db_path) as db:
                # WAL mode: allows concurrent readers while writer is active
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on lock
                await db.execute("PRAGMA synchronous=NORMAL")  # safe + faster than FULL
                await db.executescript("""
                    CREATE TABLE IF NOT EXISTS markets (
                        ticker TEXT PRIMARY KEY,
                        title TEXT,
                        category TEXT,
                        status TEXT,
                        yes_bid REAL,
                        yes_ask REAL,
                        no_bid REAL,
                        no_ask REAL,
                        volume REAL,
                        open_interest REAL,
                        close_time TEXT,
                        last_price REAL,
                        fetched_at TEXT,
                        platform TEXT DEFAULT 'kalshi'
                    );

                    CREATE TABLE IF NOT EXISTS positions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        side TEXT NOT NULL,
                        contracts INTEGER NOT NULL,
                        avg_price REAL NOT NULL,
                        current_price REAL,
                        pnl REAL DEFAULT 0,
                        status TEXT DEFAULT 'open',
                        opened_at TEXT NOT NULL,
                        closed_at TEXT,
                        close_reason TEXT
                    );

                    CREATE TABLE IF NOT EXISTS trade_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        side TEXT NOT NULL,
                        contracts INTEGER NOT NULL,
                        price REAL NOT NULL,
                        total_cost REAL NOT NULL,
                        fee REAL DEFAULT 0,
                        paper_trade INTEGER DEFAULT 1,
                        ai_confidence REAL,
                        ai_reasoning TEXT,
                        signal_source TEXT,
                        pnl REAL,
                        executed_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS paper_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        side TEXT NOT NULL,
                        price REAL NOT NULL,
                        contracts INTEGER NOT NULL,
                        ai_confidence REAL,
                        ai_reasoning TEXT,
                        arbitrage_pct REAL,
                        signal_source TEXT,
                        outcome REAL,
                        settled INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        settled_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS ai_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ticker TEXT NOT NULL,
                        action TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        reasoning TEXT,
                        model TEXT,
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        cost_usd REAL,
                        decided_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS performance_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        total_trades INTEGER DEFAULT 0,
                        winning_trades INTEGER DEFAULT 0,
                        losing_trades INTEGER DEFAULT 0,
                        total_pnl REAL DEFAULT 0,
                        win_rate REAL DEFAULT 0,
                        current_scale_factor REAL DEFAULT 1.0,
                        recorded_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS daily_stats (
                        date TEXT PRIMARY KEY,
                        trades INTEGER DEFAULT 0,
                        pnl REAL DEFAULT 0,
                        ai_cost REAL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT,
                        ticker TEXT,
                        platform TEXT,
                        side TEXT,
                        price_cents REAL,
                        size_usd REAL,
                        confidence REAL,
                        net_ev REAL,
                        reason TEXT,
                        result TEXT,
                        pnl REAL,
                        operator TEXT DEFAULT 'bot',
                        logged_at TEXT
                    );
                """)
                await db.commit()
                # Idempotent migrations for existing databases
                for migration in [
                    "ALTER TABLE trade_logs ADD COLUMN fee REAL DEFAULT 0",
                    "ALTER TABLE trade_logs ADD COLUMN platform TEXT DEFAULT 'kalshi'",
                    "ALTER TABLE positions  ADD COLUMN platform TEXT DEFAULT 'kalshi'",
                    "ALTER TABLE positions  ADD COLUMN poly_token_id TEXT",
                    "ALTER TABLE markets    ADD COLUMN platform TEXT DEFAULT 'kalshi'",
                ]:
                    try:
                        await db.execute(migration)
                        await db.commit()
                    except Exception:
                        pass  # Column already exists
            self._initialized = True
            logger.info(f"Database initialized at {self.db_path}")

    async def execute(self, query: str, params: tuple = ()) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(query, params)
            await db.commit()

    async def fetchall(self, query: str, params: tuple = ()) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def fetchone(self, query: str, params: tuple = ()) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    # ── Convenience query helpers used by dashboard / cli ────────────────────

    async def get_open_positions(self) -> list:
        return await self.fetchall("SELECT * FROM positions WHERE status='open'")

    async def get_eligible_markets(self, volume_min: float = 0,
                                    max_days_to_expiry: int = 365) -> list:
        return await self.fetchall(
            "SELECT * FROM markets WHERE status='open' AND volume >= ?",
            (volume_min,)
        )

    async def get_daily_ai_cost(self) -> float:
        from datetime import date
        today = date.today().isoformat()
        row = await self.fetchone(
            "SELECT SUM(cost_usd) as total FROM ai_decisions WHERE decided_at LIKE ?",
            (today + "%",)
        )
        return row.get("total") or 0.0 if row else 0.0

    async def insert(self, table: str, data: Dict[str, Any]) -> int:
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" * len(data))
        query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, tuple(data.values()))
            await db.commit()
            return cursor.lastrowid or 0
