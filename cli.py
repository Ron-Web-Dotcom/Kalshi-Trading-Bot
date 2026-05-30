#!/usr/bin/env python3
"""
Kalshi AI Trading Bot -- Unified CLI

Provides a single entry point for all bot operations:
    python cli.py run          Start the trading bot
    python cli.py dashboard    Launch the Streamlit monitoring dashboard
    python cli.py status       Show portfolio balance, positions, and P&L
    python cli.py backtest     Run backtests (placeholder)
    python cli.py health       Verify API connections, database, and configuration
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> None:
    """Start the trading bot (disciplined mode by default)."""
    from src.utils.logging_setup import setup_logging

    log_level = getattr(args, "log_level", "INFO")
    setup_logging(log_level=log_level)

    live = getattr(args, "live", False)
    paper = getattr(args, "paper", False)
    beast = getattr(args, "beast", False)
    disciplined = getattr(args, "disciplined", False)
    safe_compounder = getattr(args, "safe_compounder", False)

    if live and paper:
        print("Error: --live and --paper are mutually exclusive.")
        sys.exit(1)

    live_mode = live and not paper

    if live_mode:
        print("⚠️  WARNING: LIVE TRADING MODE ENABLED")
        print("   This will use real money and place actual trades.")

    # --safe-compounder mode: edge-based NO-side only
    if safe_compounder:
        _run_safe_compounder(
            live_mode=live_mode,
            loop=getattr(args, "loop", False),
            interval=getattr(args, "interval", 300),
        )
        return

    # --beast mode: original aggressive settings (NOT default)
    if beast:
        print("⚠️  BEAST MODE: Aggressive settings enabled.")
        print("   WARNING: Aggressive settings with no guardrails. Use at your own risk.")
        from beast_mode_bot import BeastModeBot
        bot = BeastModeBot(live_mode=live_mode)
        try:
            asyncio.run(bot.run())
        except KeyboardInterrupt:
            print("\nTrading bot stopped by user.")
        return

    # DEFAULT: AI directional strategy with disciplined settings active.
    # Despite earlier README copy, this is a single-model OpenRouter call
    # per decision (with a fallback chain), not a parallel ensemble.
    print("🤖  AI DIRECTIONAL MODE (default)")
    print("   Single-model OpenRouter call per decision (fallback chain on error).")
    print("   Category scoring + portfolio guardrails active.")
    print("   Use --safe-compounder for conservative math-only mode.")
    print("   Use --beast to run without guardrails (not recommended).")

    from beast_mode_bot import BeastModeBot
    from src.strategies.category_scorer import CategoryScorer
    from src.strategies.portfolio_enforcer import PortfolioEnforcer

    # Apply disciplined settings overrides
    from src.config.settings import settings as cfg
    cfg.trading.min_confidence_to_trade = 0.45
    cfg.trading.max_position_size_pct = 3.0
    cfg.trading.kelly_fraction = 0.25
    cfg.trading.max_drawdown_pct = 15.0
    cfg.trading.max_sector_exposure_pct = 30.0

    bot = BeastModeBot(live_mode=live_mode)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nTrading bot stopped by user.")


def _run_safe_compounder(
    live_mode: bool = False,
    loop: bool = False,
    interval: int = 300,
) -> None:
    """Run the Safe Compounder strategy.

    When ``loop`` is True, run the strategy repeatedly with ``interval``
    seconds between cycles until the user sends Ctrl-C.
    """
    from src.clients.kalshi_client import KalshiClient
    from src.strategies.safe_compounder import SafeCompounder

    print("🔒 SAFE COMPOUNDER MODE")
    print("   NO-side only | Edge-based | Near-certain outcomes")
    if not live_mode:
        print("   DRY RUN — no real orders will be placed")
    if loop:
        print(f"   Continuous mode — re-running every {interval}s. Ctrl-C to stop.")

    async def _run_once():
        client = KalshiClient()
        try:
            compounder = SafeCompounder(
                client=client,
                dry_run=not live_mode,
            )
            return await compounder.run()
        finally:
            await client.close()

    async def _run_forever():
        cycle = 0
        while True:
            cycle += 1
            print(f"\n──── Cycle {cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ────")
            try:
                await _run_once()
            except Exception as exc:
                # One bad cycle shouldn't kill the loop. Log and keep going.
                print(f"Cycle {cycle} failed: {exc}. Continuing after {interval}s.")
            print(f"\n⏳ Sleeping {interval}s before next cycle...")
            await asyncio.sleep(interval)

    try:
        if loop:
            asyncio.run(_run_forever())
        else:
            asyncio.run(_run_once())
    except KeyboardInterrupt:
        print("\nSafe Compounder stopped by user.")


def cmd_dashboard(args: argparse.Namespace) -> None:
    """Launch the real-time monitoring dashboard."""
    from src.utils.logging_setup import setup_logging
    setup_logging(log_level="INFO")

    # Try beast_mode_dashboard first (rich terminal UI), then bot dashboard mode
    try:
        from beast_mode_dashboard import BeastModeDashboard
        async def _run():
            d = BeastModeDashboard()
            await d.db_manager.initialize()
            await d.show_live_dashboard()
        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            print("\nDashboard stopped by user.")
    except Exception as exc:
        print(f"Dashboard error: {exc}")
        sys.exit(1)


def cmd_status(args: argparse.Namespace) -> None:
    """Show portfolio status — DB paper stats + live Kalshi balance (if reachable)."""

    async def _status() -> None:
        from src.utils.database import DatabaseManager
        from src.config.settings import settings

        db = DatabaseManager()
        await db.initialize()

        # ── Paper trade stats from local DB (always available) ───────────────
        stats = await db.fetchone("""
            SELECT
                COUNT(*)                                           AS total,
                SUM(CASE WHEN pnl > 0  THEN 1 ELSE 0 END)         AS wins,
                SUM(CASE WHEN pnl < 0  THEN 1 ELSE 0 END)         AS losses,
                SUM(CASE WHEN pnl IS NULL THEN 1 ELSE 0 END)       AS open_count,
                SUM(COALESCE(pnl, 0))                              AS total_pnl,
                SUM(COALESCE(total_cost, 0))                       AS total_cost,
                SUM(COALESCE(fee, 0))                              AS total_fees,
                AVG(ai_confidence)                                 AS avg_conf
            FROM trade_logs WHERE paper_trade=1
        """) or {}

        total     = stats.get("total") or 0
        wins      = stats.get("wins")  or 0
        losses    = stats.get("losses") or 0
        open_c    = stats.get("open_count") or 0
        total_pnl = stats.get("total_pnl")  or 0.0
        total_cost = stats.get("total_cost") or 0.0
        total_fees = stats.get("total_fees") or 0.0
        avg_conf  = stats.get("avg_conf") or 0.0
        settled   = wins + losses
        win_rate  = (wins / settled * 100) if settled > 0 else 0.0

        # Open positions
        positions = await db.fetchall(
            "SELECT ticker, side, contracts, avg_price, current_price, pnl "
            "FROM positions WHERE status='open' ORDER BY pnl DESC"
        )

        # Last 5 closed trades
        last5 = await db.fetchall("""
            SELECT ticker, action, side, price, contracts, total_cost, pnl,
                   signal_source, executed_at
            FROM trade_logs WHERE paper_trade=1 AND pnl IS NOT NULL
            ORDER BY executed_at DESC LIMIT 5
        """)

        # Markets cached count
        mkt_count = (await db.fetchone("SELECT COUNT(*) AS n FROM markets") or {}).get("n", 0)

        W = 62
        sep = "═" * W
        thin = "─" * W

        print(f"\n╔{sep}╗")
        print(f"║{'  KALSHI BOT — PAPER TRADING STATUS':^{W}}║")
        print(f"╠{sep}╣")
        mode = "PAPER (safe)" if not settings.trading.live_trading_enabled else "⚠ LIVE"
        print(f"║  Mode            : {mode:<{W-20}}║")
        print(f"║  Markets cached  : {mkt_count:<{W-20}}║")
        print(f"╠{sep}╣")
        print(f"║{'  PAPER TRADE PERFORMANCE':^{W}}║")
        print(f"╠{sep}╣")
        pnl_str = f"{'+'if total_pnl>=0 else ''}${total_pnl:.2f}"
        print(f"║  Total trades    : {total:<5}  (open: {open_c}  settled: {settled}){'':<5}║")
        print(f"║  Win / Loss      : {wins} / {losses:<{W-22}}║")
        print(f"║  Win rate        : {win_rate:.1f}%{'':<{W-22}}║")
        print(f"║  Total PnL       : {pnl_str:<{W-20}}║")
        print(f"║  Total capital   : ${total_cost:.2f}  fees: ${total_fees:.2f}{'':<{W-34}}║")
        print(f"║  Avg AI conf     : {avg_conf:.0f}%{'':<{W-22}}║")
        print(f"╠{sep}╣")

        if positions:
            print(f"║  {'OPEN POSITIONS':^{W-2}}║")
            print(f"║  {'TICKER':<28} {'SIDE':<4} {'CTR':>4} {'ENTRY':>6} {'CUR':>6} {'PnL':>8}  ║")
            print(f"║  {thin[:-2]}  ║")
            for p in positions[:10]:
                pnl_v  = p.get("pnl") or 0.0
                cur    = p.get("current_price") or p.get("avg_price") or 0
                pstr   = f"${pnl_v:+.2f}"
                print(f"║  {(p['ticker'] or '')[:28]:<28} {p['side']:<4} "
                      f"{p['contracts']:>4} {p['avg_price']:>5.0f}¢ {cur:>5.0f}¢ {pstr:>8}  ║")
        else:
            print(f"║  No open positions.{'':<{W-21}}║")

        print(f"╠{sep}╣")
        if last5:
            print(f"║  {'LAST 5 SETTLED TRADES':^{W-2}}║")
            print(f"║  {'TICKER':<26} {'ACT':<4} {'SIDE':<4} {'PRICE':>6} {'PnL':>8} {'SOURCE':<10}  ║")
            print(f"║  {thin[:-2]}  ║")
            for t in last5:
                pnl_v = t.get("pnl") or 0.0
                src   = (t.get("signal_source") or "")[:10]
                print(f"║  {(t['ticker'] or '')[:26]:<26} {t['action']:<4} {t['side']:<4} "
                      f"{t['price']:>5.0f}¢ ${pnl_v:>+7.2f} {src:<10}  ║")
        else:
            print(f"║  No settled trades yet.{'':<{W-25}}║")

        # Try live API balance (graceful fallback)
        print(f"╠{sep}╣")
        try:
            from src.clients.kalshi_client import KalshiClient
            client = KalshiClient()
            bal_resp = await client.get_balance()
            await client.close()
            cash  = (bal_resp.get("balance") or 0) / 100
            port  = (bal_resp.get("portfolio_value") or 0) / 100
            print(f"║  KALSHI ACCOUNT (live)                                        ║")
            print(f"║  Cash: ${cash:,.2f}   Portfolio value: ${port:,.2f}{'':<{W-45}}║")
        except Exception:
            print(f"║  Kalshi API: offline (paper stats above from local DB){'':<{W-55}}║")

        print(f"╚{sep}╝\n")

    asyncio.run(_status())


def cmd_scores(args: argparse.Namespace) -> None:
    """Show current category scores from the scoring system."""

    async def _scores():
        from src.strategies.category_scorer import CategoryScorer
        scorer = CategoryScorer()
        await scorer.initialize()
        scores = await scorer.get_all_scores()
        print(scorer.format_scores_table(scores))
        print()
        print("  Key: Score < 30 = BLOCKED | Alloc = max portfolio % allowed")
        print()

    try:
        asyncio.run(_scores())
    except Exception as exc:
        print(f"Error fetching scores: {exc}")
        sys.exit(1)


def cmd_history(args: argparse.Namespace) -> None:
    """Show trade history with category breakdown."""
    limit = getattr(args, "limit", 50)

    async def _history():
        import aiosqlite

        db_path = Path(__file__).parent / "trading_system.db"
        if not db_path.exists():
            print("No trading database found.")
            return

        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            # Overall stats
            cursor = await db.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl
                FROM trade_logs
            """)
            overview = await cursor.fetchone()

            print("=" * 70)
            print("  TRADE HISTORY")
            print("=" * 70)
            if overview and overview["total"]:
                total = overview["total"]
                wins = overview["wins"] or 0
                pnl = overview["total_pnl"] or 0.0
                print(f"  Total Trades:  {total}")
                print(f"  Win Rate:      {wins/total*100:.1f}%")
                print(f"  Total P&L:     ${pnl:.2f}")
                print(f"  Avg per trade: ${(pnl/total):.2f}")
            print()

            # Source breakdown (signal_source = strategy equivalent)
            cursor = await db.execute("""
                SELECT
                    signal_source as category,
                    COUNT(*) as trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(COALESCE(pnl, 0)) as total_pnl
                FROM trade_logs
                GROUP BY signal_source
                ORDER BY total_pnl DESC
            """)
            cats = await cursor.fetchall()

            if cats:
                print(f"  {'Source':<22} {'Trades':>7} {'WR':>6} {'P&L':>10}")
                print(f"  {'-'*22} {'-'*7} {'-'*6} {'-'*10}")
                for row in cats:
                    cat = row["category"] or "unknown"
                    t = row["trades"]
                    w = row["wins"] or 0
                    p = row["total_pnl"] or 0.0
                    wr = f"{w/t*100:.0f}%" if t > 0 else "n/a"
                    print(f"  {cat:<22} {t:>7} {wr:>6} ${p:>9.2f}")
                print()

            # Recent trades (uses actual schema column names)
            cursor = await db.execute(f"""
                SELECT ticker, action, side, price, contracts,
                       COALESCE(pnl, 0) as pnl, executed_at, signal_source
                FROM trade_logs
                ORDER BY executed_at DESC
                LIMIT {limit}
            """)
            trades = await cursor.fetchall()

            if trades:
                print(f"  Recent {limit} trades:")
                print(f"  {'Market':<28} {'Act':>4} {'Side':>4} {'Price':>6} {'Qty':>4} {'P&L':>8}  Source")
                print(f"  {'-'*28} {'-'*4} {'-'*4} {'-'*6} {'-'*4} {'-'*8}  {'-'*12}")
                for t in trades:
                    source = (t["signal_source"] or "")[:12]
                    pnl = t["pnl"] or 0.0
                    print(
                        f"  {t['ticker'][:28]:<28} {t['action']:>4} {t['side']:>4} "
                        f"{t['price']:>6.0f}¢ {t['contracts']:>4} ${pnl:>7.2f}  {source}"
                    )

            print("=" * 70)

    try:
        asyncio.run(_history())
    except Exception as exc:
        print(f"Error fetching history: {exc}")
        sys.exit(1)


def cmd_close_all(args: argparse.Namespace) -> None:
    """Place sell orders to close every open position on Kalshi.

    Use this AFTER stopping the bot (Ctrl-C). It queries Kalshi directly
    rather than the local DB, so it works even if local state is stale.
    Each position gets a limit sell at the current best bid for its side
    — marketable, but no guaranteed fill if the book is thin.
    """
    import uuid

    auto_yes = getattr(args, "yes", False)
    live_mode = getattr(args, "live", False)

    print("=" * 56)
    print("  CLOSE ALL POSITIONS")
    print("=" * 56)
    if not live_mode:
        print("  DRY RUN — no orders will be sent. Pass --live to actually sell.")
    print()
    print("  WARNING: this places sell orders at the current best bid.")
    print("  You may realize a loss on positions trading below entry.")
    print("  Stop the bot first (Ctrl-C) before running this command.")
    print()

    if live_mode and not auto_yes:
        confirm = input("  Type 'CLOSE ALL' to proceed: ").strip()
        if confirm != "CLOSE ALL":
            print("  Aborted.")
            return

    async def _close() -> None:
        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()
        try:
            positions_resp = await client.get_positions()
            market_positions = [
                p for p in positions_resp.get("market_positions", [])
                if p.get("position", 0) != 0
            ]

            if not market_positions:
                print("  No open positions on Kalshi.")
                return

            print(f"  Found {len(market_positions)} open position(s).")
            print()

            placed = 0
            failed = 0
            for pos in market_positions:
                ticker = pos["ticker"]
                contracts = pos["position"]            # signed: + YES, - NO
                side = "yes" if contracts > 0 else "no"
                quantity = abs(contracts)

                try:
                    book_resp = await client.get_orderbook(ticker, depth=1)
                    book = book_resp.get("orderbook", {})
                    side_bids = book.get(side, [])
                    if not side_bids:
                        print(f"  ⚠️  {ticker}: no {side.upper()} bids in book — skipping")
                        failed += 1
                        continue
                    # Kalshi orderbook bid entries are [price_cents, count].
                    # Best bid is the highest price.
                    best_bid_cents = max(int(level[0]) for level in side_bids)
                except Exception as exc:
                    print(f"  ❌ {ticker}: orderbook fetch failed — {exc}")
                    failed += 1
                    continue

                if not live_mode:
                    print(
                        f"  [DRY] would sell {quantity} {side.upper()} of {ticker} "
                        f"at {best_bid_cents}¢ (~${best_bid_cents * quantity / 100:.2f})"
                    )
                    placed += 1
                    continue

                order_params = {
                    "ticker": ticker,
                    "client_order_id": str(uuid.uuid4()),
                    "side": side,
                    "action": "sell",
                    "count": quantity,
                    "type_": "limit",
                }
                if side == "yes":
                    order_params["yes_price"] = best_bid_cents
                else:
                    order_params["no_price"] = best_bid_cents

                try:
                    resp = await client.place_order(**order_params)
                    if resp and "order" in resp:
                        print(
                            f"  ✅ {ticker}: sell {quantity} {side.upper()} "
                            f"@ {best_bid_cents}¢ — order_id={resp['order'].get('order_id', '?')}"
                        )
                        placed += 1
                    else:
                        print(f"  ❌ {ticker}: unexpected response {resp}")
                        failed += 1
                except Exception as exc:
                    print(f"  ❌ {ticker}: order failed — {exc}")
                    failed += 1

            print()
            print(f"  Placed: {placed} | Failed: {failed}")
            print()
            print("  Sell orders are limit-priced — they may rest unfilled if the book")
            print("  moves. Check Kalshi or `python cli.py status` after a minute.")
        finally:
            await client.close()

    try:
        asyncio.run(_close())
    except Exception as exc:
        print(f"  Error: {exc}")
        sys.exit(1)


def cmd_backtest(args: argparse.Namespace) -> None:
    """Run backtests (placeholder)."""
    print("=" * 56)
    print("  BACKTESTING")
    print("=" * 56)
    print()
    print("  Backtesting engine coming soon.")
    print()
    print("  Planned features:")
    print("    - Historical market replay")
    print("    - Strategy parameter optimization")
    print("    - Walk-forward analysis")
    print("    - Monte Carlo simulation")
    print()
    print("=" * 56)


def cmd_health(args: argparse.Namespace) -> None:
    """Run health checks on configuration, API, and database."""

    checks_passed = 0
    checks_failed = 0

    def ok(label: str, detail: str = "") -> None:
        nonlocal checks_passed
        checks_passed += 1
        suffix = f" -- {detail}" if detail else ""
        print(f"  [PASS] {label}{suffix}")

    def fail(label: str, detail: str = "") -> None:
        nonlocal checks_failed
        checks_failed += 1
        suffix = f" -- {detail}" if detail else ""
        print(f"  [FAIL] {label}{suffix}")

    print("=" * 56)
    print("  HEALTH CHECK")
    print("=" * 56)
    print()

    # 1. .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        ok(".env file exists")
    else:
        fail(".env file missing", "copy env.template to .env and fill in keys")

    # 2. Required environment variables
    from dotenv import load_dotenv
    load_dotenv()

    for var, placeholder in (
        ("KALSHI_API_KEY_ID", "your-key-id-uuid-here"),
        ("ANTHROPIC_API_KEY", "your_anthropic_api_key_here"),
    ):
        val = os.getenv(var, "")
        if val and val not in ("", placeholder):
            ok(f"{var} is set")
        else:
            fail(f"{var} is missing or placeholder")

    # 3. Kalshi API connection
    async def _check_api() -> None:
        from src.clients.kalshi_client import KalshiClient
        client = KalshiClient()
        try:
            balance_resp = await client.get_balance()
            balance_usd = balance_resp.get("balance", 0) / 100.0
            ok("Kalshi API connection", f"balance=${balance_usd:,.2f}")
        except Exception as exc:
            msg = str(exc)
            fail("Kalshi API connection", msg)
            if "401" in msg or "authentication" in msg.lower():
                print(
                    "         A 401 from Kalshi almost always means one of:\n"
                    "           - KALSHI_API_KEY in .env doesn't match the API key ID on Kalshi\n"
                    "           - The private key file (default: kalshi_private_key.pem) is the\n"
                    "             wrong key for that API key, or its path is wrong\n"
                    "           - The API key was created on the Kalshi demo env but you're\n"
                    "             pointing at production (or vice versa)\n"
                    "         Re-download the key pair from Kalshi and verify both KALSHI_API_KEY\n"
                    "         and KALSHI_PRIVATE_KEY_PATH (if set) point to the matching pair."
                )
        finally:
            await client.close()

    try:
        asyncio.run(_check_api())
    except Exception as exc:
        fail("Kalshi API connection", str(exc))

    # 4. Database
    db_path = Path(__file__).parent / "trading_system.db"
    try:
        import aiosqlite

        async def _check_db() -> None:
            from src.utils.database import DatabaseManager
            db_manager = DatabaseManager()
            await db_manager.initialize()
            ok("Database initialization", str(db_path))

        asyncio.run(_check_db())
    except Exception as exc:
        fail("Database initialization", str(exc))

    # 5. Python version (3.10+ required; 3.12+ recommended)
    vi = sys.version_info
    ver_str = f"{vi.major}.{vi.minor}.{vi.micro}"
    if vi >= (3, 10):
        ok("Python version", ver_str + (" (3.12+ recommended)" if vi < (3, 12) else ""))
    else:
        fail("Python version", f"requires >=3.10, found {ver_str}")

    # Summary
    print()
    total = checks_passed + checks_failed
    print(f"  {checks_passed}/{total} checks passed")
    if checks_failed:
        print(f"  {checks_failed} issue(s) need attention")
    else:
        print("  All systems operational.")
    print("=" * 56)

    if checks_failed:
        sys.exit(1)


def cmd_test_discord(args: argparse.Namespace) -> None:
    """Send a test alert to Discord webhook."""
    from src.alerts.discord import DiscordAlerter
    from src.config.settings import settings

    webhook = settings.alerts.discord_webhook_url
    if not webhook:
        print("DISCORD_WEBHOOK_URL is not set in .env")
        print("Add it: DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK")
        sys.exit(1)

    print(f"Sending test alert to webhook (last 10 chars: ...{webhook[-10:]})")

    async def _run():
        discord = DiscordAlerter()
        mode = "LIVE" if settings.trading.live_trading_enabled else "PAPER"
        ok = await discord.test_alert(mode=mode)
        if ok:
            print("✅  Test alert delivered — check your Discord channel")
        else:
            print("❌  Delivery failed — check the webhook URL and try again")
            sys.exit(1)

    asyncio.run(_run())


def cmd_live_check(args: argparse.Namespace) -> None:
    """Run live trading pre-flight checks."""
    from src.execution.preflight import run_preflight, print_preflight_report

    print("Running pre-flight checks for live trading...")

    async def _run():
        passed, results, balance = await run_preflight(verbose=True)
        print_preflight_report(results, passed, balance)
        if not passed:
            sys.exit(1)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kalshi-bot",
        description="Kalshi AI Trading Bot -- Multi-model AI trading for prediction markets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python cli.py run                      Start AI Ensemble mode (default, paper)\n"
            "  python cli.py run --live               AI Ensemble with real capital\n"
            "  python cli.py run --safe-compounder    Safe Compounder: conservative, math-only\n"
            "  python cli.py run --safe-compounder --live  Safe Compounder live\n"
            "  python cli.py run --beast              Beast mode (aggressive, not recommended)\n"
            "  python cli.py scores                   Show category scores\n"
            "  python cli.py history                  Show trade history + category breakdown\n"
            "  python cli.py status                   Check portfolio balance and positions\n"
            "  python cli.py health                   Verify all connections and config\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    p_run = subparsers.add_parser(
        "run",
        help="Start the trading bot (disciplined mode by default)",
        description=(
            "Launch one of the example trading strategies. Default is the AI "
            "directional strategy: a single LLM call per market via OpenRouter "
            "(fallback chain on error), with category scoring and portfolio "
            "guardrails layered on top. Use --safe-compounder for the "
            "conservative math-only NO-side strategy. Use --beast to run "
            "without guardrails (not recommended). All three are starting "
            "points — fork them, tune them, replace them."
        ),
    )
    live_group = p_run.add_mutually_exclusive_group()
    live_group.add_argument(
        "--live",
        action="store_true",
        help="Enable live trading with real capital (default: paper trading)",
    )
    live_group.add_argument(
        "--paper",
        action="store_true",
        help="Run in paper-trading mode (no real orders)",
    )
    strategy_group = p_run.add_mutually_exclusive_group()
    strategy_group.add_argument(
        "--disciplined",
        action="store_true",
        default=True,
        help="Disciplined mode: category scoring + portfolio enforcement (DEFAULT)",
    )
    strategy_group.add_argument(
        "--beast",
        action="store_true",
        help="Beast mode: aggressive settings, no guardrails (not recommended)",
    )
    strategy_group.add_argument(
        "--safe-compounder",
        action="store_true",
        dest="safe_compounder",
        help="Safe Compounder: NO-side only, edge-based, near-certain outcomes",
    )
    p_run.add_argument(
        "--loop",
        action="store_true",
        help="Re-run the strategy continuously (only honored by --safe-compounder today)",
    )
    p_run.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between cycles when --loop is set (default: 300)",
    )
    p_run.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging verbosity (default: INFO)",
    )
    p_run.set_defaults(func=cmd_run)

    # --- scores ---
    p_scores = subparsers.add_parser(
        "scores",
        help="Show current category scores",
        description="Display all trading category scores, win rates, ROI, and allocation limits.",
    )
    p_scores.set_defaults(func=cmd_scores)

    # --- history ---
    p_history = subparsers.add_parser(
        "history",
        help="Show trade history with category breakdown",
        description="Display closed trade history grouped by category, win rate, and P&L.",
    )
    p_history.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of recent trades to show (default: 50)",
    )
    p_history.set_defaults(func=cmd_history)

    # --- dashboard ---
    p_dash = subparsers.add_parser(
        "dashboard",
        help="Launch the Streamlit monitoring dashboard",
        description="Open a real-time web dashboard showing portfolio performance, positions, risk metrics, and AI decision logs.",
    )
    p_dash.set_defaults(func=cmd_dashboard)

    # --- status ---
    p_status = subparsers.add_parser(
        "status",
        help="Show portfolio balance, positions, and P&L",
        description="Connect to the Kalshi API and display current account balance, open positions, and estimated portfolio value.",
    )
    p_status.set_defaults(func=cmd_status)

    # --- backtest ---
    p_bt = subparsers.add_parser(
        "backtest",
        help="Run backtests (coming soon)",
        description="Backtest trading strategies against historical market data. This feature is under development.",
    )
    p_bt.set_defaults(func=cmd_backtest)

    # --- health ---
    p_health = subparsers.add_parser(
        "health",
        help="Verify API connections, database, and configuration",
        description="Run a series of diagnostic checks: .env presence, API key configuration, Kalshi API connectivity, database initialization, and Python version.",
    )
    p_health.set_defaults(func=cmd_health)

    # --- close-all ---
    p_close = subparsers.add_parser(
        "close-all",
        help="Place limit sell orders to close every open position on Kalshi",
        description=(
            "Best-effort liquidation: query Kalshi for open positions and place "
            "a limit sell at the current best bid for each. Run this AFTER stopping "
            "the bot. Defaults to dry-run; pass --live to actually send orders."
        ),
    )
    p_close.add_argument(
        "--live",
        action="store_true",
        help="Actually place sell orders (default is a dry-run preview)",
    )
    p_close.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive 'CLOSE ALL' confirmation (dangerous)",
    )
    p_close.set_defaults(func=cmd_close_all)

    # --- test-discord ---
    p_discord = subparsers.add_parser(
        "test-discord",
        help="Send a test message to your Discord webhook",
        description="Verifies DISCORD_WEBHOOK_URL is correct by delivering a test embed.",
    )
    p_discord.set_defaults(func=cmd_test_discord)

    # --- live-check ---
    p_live = subparsers.add_parser(
        "live-check",
        help="Run pre-flight safety checks before enabling live trading",
        description=(
            "Verifies all requirements for live trading: API keys, Kalshi "
            "connectivity, account balance, paper trade history, Discord alerts, "
            "and .env settings. Fix every FAIL before setting LIVE_TRADING_ENABLED=true."
        ),
    )
    p_live.set_defaults(func=cmd_live_check)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
