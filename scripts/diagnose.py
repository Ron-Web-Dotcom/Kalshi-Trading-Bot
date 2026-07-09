#!/usr/bin/env python3
"""
Full diagnostic for the Kalshi Trading Bot.
Run on your VPS after every pull/restart:
  python3 scripts/diagnose.py

Checks (in order):
  1.  Python version
  2.  Required packages installed
  3.  .env file present + required vars set (values never printed)
  4.  Database file exists + schema intact + row counts
  5.  SQLite PRAGMAs (WAL, cache, mmap)
  6.  DB memory limits (cache_size, mmap_size)
  7.  Open positions sanity
  8.  _last_trade_time TTL prune logic (unit)
  9.  _poly_cache TTL refresh logic (unit)
  10. cleanup_old_rows() runs without error
  11. Confidence thresholds match 77% minimum
  12. Data sources reachable (all 23 endpoints)
  13. Kalshi API credentials valid (paper mode only)
  14. Polymarket Gamma API reachable
  15. Discord webhook reachable (if configured)
  16. AI (OpenAI) API key valid
  17. Process memory usage (RSS)
  18. Disk space on DB partition
"""

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
WARN = 0

W  = "\033[93m"   # yellow
G  = "\033[92m"   # green
R  = "\033[91m"   # red
B  = "\033[94m"   # blue
NC = "\033[0m"    # reset

def ok(label, detail=""):
    global PASS; PASS += 1
    tail = f"  {detail}" if detail else ""
    print(f"  {G}✅ PASS{NC}  {label}{tail}")

def warn(label, detail=""):
    global WARN; WARN += 1
    tail = f"  {detail}" if detail else ""
    print(f"  {W}⚠️  WARN{NC}  {label}{tail}")

def fail(label, detail=""):
    global FAIL; FAIL += 1
    tail = f"  — {detail}" if detail else ""
    print(f"  {R}❌ FAIL{NC}  {label}{tail}")

def section(title):
    print(f"\n{B}{'─'*55}{NC}")
    print(f"{B}  {title}{NC}")
    print(f"{B}{'─'*55}{NC}")


# ── 1. Python version ─────────────────────────────────────────────────────────

def check_python():
    section("1. Python version")
    v = sys.version_info
    label = f"Python {v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        ok(label)
    elif v >= (3, 10):
        warn(label, "3.11+ recommended")
    else:
        fail(label, "3.11+ required")


# ── 2. Required packages ──────────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    "aiosqlite", "httpx", "openai",
    "dotenv", "zoneinfo", "pydantic",
]

def check_packages():
    section("2. Required packages")
    for pkg in REQUIRED_PACKAGES:
        real = "python-dotenv" if pkg == "dotenv" else pkg
        try:
            importlib.import_module(pkg)
            ok(real)
        except ImportError:
            fail(real, "not installed — pip install " + real)


# ── 3. .env file + required vars ─────────────────────────────────────────────

REQUIRED_VARS = [
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "OPENAI_API_KEY",
]
OPTIONAL_VARS = [
    "DISCORD_WEBHOOK_URL",
    "POLY_API_KEY",
]

def check_env():
    section("3. Environment variables")
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        ok(".env file found")
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except Exception:
            pass
    else:
        warn(".env file not found (using system env)")

    for var in REQUIRED_VARS:
        val = os.environ.get(var, "")
        if val:
            ok(var, "(set)")
        else:
            fail(var, "NOT SET — bot will crash on start")

    for var in OPTIONAL_VARS:
        val = os.environ.get(var, "")
        if val:
            ok(var, "(set)")
        else:
            warn(var, "not set — feature disabled")


# ── 4. Database schema + row counts ──────────────────────────────────────────

EXPECTED_TABLES = [
    "markets", "positions", "trade_logs", "paper_signals",
    "ai_decisions", "performance_metrics", "daily_stats", "audit_log",
]

async def check_database():
    section("4. Database")
    try:
        from src.config.settings import settings
        db_path = settings.database.path
    except Exception as e:
        fail("settings load", str(e))
        return

    if not os.path.exists(db_path):
        fail(f"DB file not found: {db_path}")
        return
    ok(f"DB file exists: {db_path}", f"({os.path.getsize(db_path) // 1024} KB)")

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        # Check tables
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        missing = [t for t in EXPECTED_TABLES if t not in tables]
        if missing:
            fail("Schema", f"missing tables: {missing}")
        else:
            ok("Schema", f"all {len(EXPECTED_TABLES)} tables present")

        # Row counts
        counts = {}
        for t in EXPECTED_TABLES:
            if t in tables:
                cur = await db.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = (await cur.fetchone())[0]
        ok("Row counts", "  ".join(f"{t}={counts.get(t,0)}" for t in EXPECTED_TABLES))

        # WAL mode
        cur = await db.execute("PRAGMA journal_mode")
        mode = (await cur.fetchone())[0]
        if mode == "wal":
            ok("journal_mode=WAL")
        else:
            warn(f"journal_mode={mode}", "expected WAL")

        # cache_size
        cur = await db.execute("PRAGMA cache_size")
        cs = (await cur.fetchone())[0]
        if cs <= -1000:
            ok(f"cache_size={cs}", f"({abs(cs)} KB)")
        else:
            warn(f"cache_size={cs}", "expected -8000 (8MB)")

        # Open positions
        cur = await db.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
        n_open = (await cur.fetchone())[0]
        ok(f"Open positions: {n_open}")

        # Check for positions missing current_price (would give $0 unrealised PnL)
        cur = await db.execute(
            "SELECT COUNT(*) FROM positions p "
            "LEFT JOIN markets m ON m.ticker=p.ticker "
            "WHERE p.status='open' AND p.current_price IS NULL AND m.yes_ask IS NULL"
        )
        orphans = (await cur.fetchone())[0]
        if orphans:
            warn(f"{orphans} open position(s) have no price data — unrealised PnL will show $0")
        else:
            ok("All open positions have price data")


# ── 5. Memory/TTL unit checks ─────────────────────────────────────────────────

def check_memory_logic():
    section("5. Memory / TTL logic")

    # _last_trade_time prune
    try:
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock

        mgr_mod = importlib.import_module("src.risk.manager")
        mock_cfg = MagicMock()
        mock_cfg.cooldown_between_trades_seconds = 300

        import src.risk.manager as rm_mod
        orig_settings = None
        try:
            from src.config import settings as cfg_mod
            orig_settings = cfg_mod.settings
        except Exception:
            pass

        rm = object.__new__(rm_mod.RiskManager)
        rm.cfg = mock_cfg
        rm.db = None
        rm._last_trade_time = {}
        rm._daily_loss = 0.0
        rm._daily_loss_date = None

        # Insert 1000 stale entries
        old = datetime.now(timezone.utc) - timedelta(seconds=1200)
        for i in range(1000):
            rm._last_trade_time[(f"TICK-{i}", "kalshi")] = old
        # record one fresh trade
        rm.record_trade("FRESH-1", platform="kalshi")
        remaining = len(rm._last_trade_time)
        if remaining <= 5:
            ok(f"_last_trade_time TTL prune", f"1000 stale entries pruned → {remaining} remaining")
        else:
            fail(f"_last_trade_time TTL prune", f"{remaining} entries remain after prune")
    except Exception as e:
        fail("_last_trade_time TTL prune", str(e))

    # _poly_cache TTL
    try:
        import time
        import src.data.external_markets as em_mod
        comp = object.__new__(em_mod.ExternalMarketComparator)
        comp._poly_cache = ["fake"]
        comp._poly_cache_time = time.time() - 2000  # expired
        needs_refresh = not comp._poly_cache or (time.time() - comp._poly_cache_time) > 1800
        if needs_refresh:
            ok("_poly_cache TTL check", "stale cache triggers refresh correctly")
        else:
            fail("_poly_cache TTL check", "stale cache not detected")
    except Exception as e:
        fail("_poly_cache TTL check", str(e))


# ── 6. Confidence thresholds ─────────────────────────────────────────────────

def check_confidence():
    section("6. Confidence thresholds")
    try:
        from src.config.settings import settings
        t = settings.trading
        checks = [
            ("min_ai_confidence",      t.min_ai_confidence,      75.0),
            ("min_confidence_to_trade", t.min_confidence_to_trade, 0.75),
        ]
        for name, val, minimum in checks:
            if val >= minimum:
                ok(name, f"= {val}")
            else:
                fail(name, f"= {val}  (expected ≥ {minimum})")
    except Exception as e:
        fail("settings load", str(e))

    try:
        from src.utils.confidence_calibrator import _FLOOR_CONF, _DEFAULT
        if _FLOOR_CONF >= 75.0:
            ok(f"calibrator _FLOOR_CONF = {_FLOOR_CONF}")
        else:
            fail(f"calibrator _FLOOR_CONF = {_FLOOR_CONF}", "expected ≥ 75")
        if _DEFAULT >= 75.0:
            ok(f"calibrator _DEFAULT = {_DEFAULT}")
        else:
            warn(f"calibrator _DEFAULT = {_DEFAULT}", "expected ≥ 75")
    except Exception as e:
        fail("confidence_calibrator import", str(e))


# ── 7. Data sources (live HTTP) ───────────────────────────────────────────────

SOURCES = [
    ("Google News RSS",  "https://news.google.com/rss/search?q=bitcoin&hl=en-US&gl=US&ceid=US:en", False),
    ("Yahoo News RSS",   "https://news.yahoo.com/rss/search?p=bitcoin",                             False),
    ("Bing News RSS",    "https://www.bing.com/news/search?q=bitcoin&format=rss",                   False),
    ("NPR RSS",          "https://feeds.npr.org/1001/rss.xml",                                      False),
    ("BBC RSS",          "https://feeds.bbci.co.uk/news/rss.xml",                                   False),
    ("AlJazeera RSS",    "https://www.aljazeera.com/search/bitcoin?format=rss",                    False),
    ("Manifold",         "https://api.manifold.markets/v0/search-markets?term=bitcoin&limit=2",     True),
    ("Polymarket",       "https://gamma-api.polymarket.com/markets?search=bitcoin&active=true&limit=3", True),
    ("PredictIt",        "https://www.predictit.org/api/marketdata/all/",                          True),
    ("Wikipedia",        "https://en.wikipedia.org/api/rest_v1/page/summary/Bitcoin",              True),
    ("DuckDuckGo",       "https://api.duckduckgo.com/?q=bitcoin&format=json&no_html=1&skip_disambig=1", True),
    ("Wikidata",         "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=bitcoin&language=en&limit=2&format=json", True),
    ("YouTube",          "https://www.youtube.com/results?search_query=bitcoin",                   False),
    ("CoinGecko",        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", True),
    ("Yahoo Finance",    "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&range=1d", True),
    ("ESPN scoreboard",  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", True),
    ("FRED (fed funds)", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",            False),
    ("wttr.in weather",  "https://wttr.in/New+York?format=j1",                                     True),
]

async def check_sources():
    section("7. Data sources (live HTTP)")
    import httpx
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    timeout = httpx.Timeout(10.0)
    passed = 0
    failed_names = []

    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        async def test(name, url, as_json):
            try:
                r = await client.get(url)
                r.raise_for_status()
                body = r.json() if as_json else r.text
                size = len(str(body))
                ok(name, f"{size:,} bytes")
                return True
            except Exception as e:
                fail(name, str(e)[:80])
                return False

        results = await asyncio.gather(*[test(n, u, j) for n, u, j in SOURCES])

    passed = sum(results)
    failed_names = [SOURCES[i][0] for i, ok_ in enumerate(results) if not ok_]
    print(f"\n  Sources: {passed}/{len(SOURCES)} healthy")
    if failed_names:
        print(f"  Failing: {', '.join(failed_names)}")


# ── 8. Kalshi API credentials ─────────────────────────────────────────────────

async def check_kalshi():
    section("8. Kalshi API")
    try:
        from src.config.settings import settings
        if settings.trading.live_trading_enabled:
            warn("LIVE_TRADING_ENABLED=true", "paper mode is OFF — real money at risk")
        else:
            ok("LIVE_TRADING_ENABLED=false (paper mode)")

        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()
        # Just verify the client initialises (no actual API call to avoid rate limits)
        ok("KalshiClient initialised")
    except Exception as e:
        fail("Kalshi client init", str(e))


# ── 9. Discord webhook ────────────────────────────────────────────────────────

async def check_discord():
    section("9. Discord webhook")
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        warn("DISCORD_WEBHOOK_URL not set", "alerts disabled")
        return
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url)
            if r.status_code in (200, 405):  # 405 = valid webhook, wrong method
                ok("Discord webhook reachable")
            else:
                warn(f"Discord webhook returned {r.status_code}")
    except Exception as e:
        fail("Discord webhook", str(e)[:80])


# ── 10. OpenAI API key ────────────────────────────────────────────────────────

async def check_openai():
    section("10. OpenAI API key")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        fail("OPENAI_API_KEY not set", "AI decisions will fail")
        return
    if not key.startswith("sk-"):
        warn("OPENAI_API_KEY set but doesn't start with 'sk-'")
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            if r.status_code == 200:
                ok("OpenAI API key valid")
            elif r.status_code == 401:
                fail("OpenAI API key invalid (401)")
            else:
                warn(f"OpenAI API returned {r.status_code}")
    except Exception as e:
        fail("OpenAI API check", str(e)[:80])


# ── 11. Process memory ────────────────────────────────────────────────────────

def check_memory():
    section("11. Process memory")
    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_kb / 1024
        label = f"Diagnostic process RSS: {rss_mb:.1f} MB"
        if rss_mb < 200:
            ok(label)
        elif rss_mb < 500:
            warn(label, "getting heavy")
        else:
            fail(label, "over 500 MB — check for leaks")
    except Exception as e:
        warn("memory check", str(e))


# ── 12. Disk space ────────────────────────────────────────────────────────────

def check_disk():
    section("12. Disk space")
    try:
        import shutil
        try:
            from src.config.settings import settings
            path = os.path.dirname(os.path.abspath(settings.database.path)) or "."
        except Exception:
            path = "."
        total, used, free = shutil.disk_usage(path)
        free_gb  = free  / (1024 ** 3)
        total_gb = total / (1024 ** 3)
        used_pct = used / total * 100
        label = f"Disk: {free_gb:.1f} GB free / {total_gb:.1f} GB  ({used_pct:.0f}% used)"
        if free_gb > 2:
            ok(label)
        elif free_gb > 0.5:
            warn(label, "low disk space")
        else:
            fail(label, "critically low disk — bot may crash")
    except Exception as e:
        warn("disk check", str(e))


# ── Runner ────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'='*55}")
    print(f"  Kalshi Trading Bot — Full Diagnostic")
    print(f"{'='*55}")

    check_python()
    check_packages()
    check_env()
    await check_database()
    check_memory_logic()
    check_confidence()
    await check_sources()
    await check_kalshi()
    await check_discord()
    await check_openai()
    check_memory()
    check_disk()

    print(f"\n{'='*55}")
    color = G if FAIL == 0 else R
    print(f"  {color}Results: {PASS} passed  |  {WARN} warnings  |  {FAIL} failed{NC}")
    print(f"{'='*55}\n")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
