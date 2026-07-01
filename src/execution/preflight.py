"""Pre-flight checks before enabling live trading."""

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger("trading.preflight")


async def run_preflight(verbose: bool = True) -> Tuple[bool, List[str], Optional[float]]:
    """
    Run all safety checks required before live trading.
    Returns (all_passed, list_of_results).
    """
    from src.config.settings import settings
    from src.clients.kalshi_client import KalshiClient
    from src.utils.database import DatabaseManager
    from src.alerts.discord import DiscordAlerter

    results: List[str] = []
    passed = True

    def ok(msg: str):
        results.append(f"  PASS  {msg}")

    def fail(msg: str):
        nonlocal passed
        passed = False
        results.append(f"  FAIL  {msg}")

    def warn(msg: str):
        results.append(f"  WARN  {msg}")

    # 1. OpenAI API key present
    if settings.ai.openai_api_key:
        ok("OPENAI_API_KEY is set")
    else:
        warn("OPENAI_API_KEY not set — AI decisions will use rule-based fallback")

    # 2. Kalshi RSA key loadable
    try:
        pem = settings.kalshi.private_key_pem
        if not pem and settings.kalshi.private_key_path:
            with open(settings.kalshi.private_key_path) as f:
                pem = f.read()
        if pem and settings.kalshi.api_key_id:
            ok("Kalshi RSA key loaded")
        else:
            fail("Kalshi RSA key or API key ID missing")
    except Exception as e:
        fail(f"Kalshi RSA key load error: {e}")

    # 3. Kalshi API connectivity + balance
    balance_usd = None
    kalshi = KalshiClient()
    try:
        bal = await kalshi.get_balance()
        raw = bal.get("balance", bal.get("available_balance_cents"))
        if raw is None:
            raise ValueError(f"No balance key found in API response: {list(bal.keys())}")
        if not isinstance(raw, (int, float)):
            raise ValueError(f"Unexpected balance type {type(raw).__name__}: {raw!r}")
        # Kalshi returns balance in cents
        balance_usd = raw / 100
        if balance_usd and balance_usd > 0:
            ok(f"Kalshi API connected | Balance: ${balance_usd:.2f}")
        else:
            warn(f"Kalshi API connected but balance is ${balance_usd:.2f} — fund your account")
    except Exception as e:
        fail(f"Kalshi API unreachable: {e}")
    finally:
        await kalshi.close()

    # 4. Database initialized and writable
    try:
        db = DatabaseManager()
        await db.initialize()
        count = await db.fetchone("SELECT COUNT(*) as n FROM markets")
        market_count = count.get("n", 0) if count else 0
        if market_count > 0:
            ok(f"Database ready | {market_count} markets cached")
        else:
            warn("Database initialized but no markets cached — run ingest first")
    except Exception as e:
        fail(f"Database error: {e}")

    # 5. Paper trade history sanity check
    try:
        db = DatabaseManager()
        await db.initialize()
        stats = await db.fetchone("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(COALESCE(pnl, 0)) as total_pnl
            FROM trade_logs WHERE paper_trade=1 AND pnl IS NOT NULL
        """)
        total = stats.get("total") or 0
        wins = stats.get("wins") or 0
        pnl = stats.get("total_pnl") or 0.0
        win_rate = (wins / total * 100) if total > 0 else 0
        if total == 0:
            warn("No completed paper trades yet — strongly recommend paper trading first")
        elif win_rate < 40:
            fail(f"Paper win rate is {win_rate:.0f}% ({total} trades, PnL=${pnl:+.2f}) — needs improvement")
        else:
            ok(f"Paper trading: {total} trades, {win_rate:.0f}% win rate, PnL=${pnl:+.2f}")
    except Exception as e:
        warn(f"Could not read paper trade history: {e}")

    # 6. Discord alert test
    try:
        discord = DiscordAlerter()
        if discord.enabled:
            delivered = await discord.test_alert(mode="LIVE PRE-FLIGHT")
            if delivered:
                ok("Discord webhook: test alert delivered")
            else:
                warn("Discord webhook configured but delivery failed")
        else:
            warn("DISCORD_WEBHOOK_URL not set — no trade alerts will be sent")
    except Exception as e:
        warn(f"Discord test failed: {e}")

    # 7. Live trading flag check
    if settings.trading.live_trading_enabled:
        ok("LIVE_TRADING_ENABLED=true in .env")
    else:
        fail("LIVE_TRADING_ENABLED=false — set it to true in .env to trade live")

    if settings.kalshi.use_demo:
        fail("KALSHI_USE_DEMO=true — set it to false for live trading on real account")
    else:
        ok("KALSHI_USE_DEMO=false (production API)")

    return passed, results, balance_usd


def print_preflight_report(results: List[str], passed: bool, balance_usd=None):
    print("\n" + "=" * 60)
    print("  KALSHI BOT — LIVE TRADING PRE-FLIGHT CHECKLIST")
    print("=" * 60)
    for line in results:
        print(line)
    print("=" * 60)
    if passed:
        print("  ALL CHECKS PASSED — Ready for live trading")
        if balance_usd:
            print(f"  Account balance: ${balance_usd:.2f}")
    else:
        print("  PRE-FLIGHT FAILED — Fix FAIL items before going live")
    print("=" * 60 + "\n")
