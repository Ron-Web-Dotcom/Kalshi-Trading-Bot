"""
Polymarket full API diagnostic — run on VPS:
    python3 test_poly.py

Checks:
  1. Gamma API  — market data (public, no auth)
  2. CLOB API   — balance, open orders, positions (requires key + secret)
"""
import asyncio
import sys
import time
sys.path.insert(0, "/root/trading-bot")


async def main():
    import httpx
    from src.clients.polymarket_client import PolymarketTradingClient, GAMMA_BASE, CLOB_BASE
    from src.config.settings import settings
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    c      = PolymarketTradingClient()
    now_et = datetime.now(_ET)

    # ─────────────────────────────────────────────────────────────────────
    print("=" * 60)
    print("  POLYMARKET API DIAGNOSTIC")
    print(f"  {now_et.strftime('%A %B %d %Y %I:%M %p ET')}")
    print("=" * 60)

    # ── Config ──────────────────────────────────────────────────────────
    print("\n── Config ──────────────────────────────────────────────────")
    print(f"  POLY_API_KEY set     : {bool(settings.polymarket.api_key)}")
    print(f"  POLY_API_SECRET set  : {bool(settings.polymarket.api_secret)}")
    print(f"  POLY_WALLET_ADDRESS  : {settings.polymarket.wallet_address or '❌ NOT SET'}")
    print(f"  POLY_ENABLED         : {settings.polymarket.enabled}")
    print(f"  POLY_LIVE_TRADING    : {settings.polymarket.live_trading_enabled}")

    # ── Gamma API — market data ──────────────────────────────────────────
    print("\n── Gamma API (market data, public) ─────────────────────────")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
            r = await hc.get(f"{GAMMA_BASE}/markets", params={"active": "true", "closed": "false", "limit": 1})
        if r.status_code == 200:
            print(f"  ✅ Gamma API reachable — HTTP {r.status_code}")
        else:
            print(f"  ❌ Gamma API HTTP {r.status_code}")
    except Exception as e:
        print(f"  ❌ Gamma API error: {e}")

    # Live markets right now (closing within 2h)
    try:
        live = await c.get_live_now_markets(max_markets=500)
        print(f"  Live now (≤2h):  {len(live)} markets")
        for m in live[:5]:
            print(f"    {m.get('close_time','?')[:16]}  YES={m.get('yes_ask',0):>3.0f}¢  {m.get('title','?')[:50]}")
        if not live:
            print("    (no markets closing within 2h right now)")
    except Exception as e:
        print(f"  ❌ Live markets error: {e}")

    # Today's markets
    try:
        today = await c.get_markets(limit=500)
        print(f"  Closing today:   {len(today)} markets")
        for m in today[:3]:
            print(f"    {m.get('close_time','?')[:16]}  YES={m.get('yes_ask',0):>3.0f}¢  {m.get('title','?')[:50]}")
    except Exception as e:
        print(f"  ❌ Today markets error: {e}")

    # ── CLOB API — authenticated ─────────────────────────────────────────
    print("\n── CLOB API (authenticated) ─────────────────────────────────")

    if not settings.polymarket.api_key or not settings.polymarket.api_secret:
        print("  ❌ POLY_API_KEY or POLY_API_SECRET missing — skipping CLOB checks")
    else:
        # Balance
        try:
            bal = await c.get_balance()
            if bal is not None:
                print(f"  ✅ USDC Balance  : ${bal:.2f}")
            else:
                print("  ❌ Balance returned None — key/secret may be wrong")
        except Exception as e:
            print(f"  ❌ Balance error: {e}")

        # Open orders on CLOB
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
                path = "/orders"
                ts   = str(int(time.time() * 1000))
                from src.clients.polymarket_client import PolymarketTradingClient as _PC
                _tmp = _PC()
                sig  = _tmp._sign(ts, "GET", path)
                headers = {
                    "X-PM-Access-Key": settings.polymarket.api_key,
                    "X-PM-Timestamp":  ts,
                    "X-PM-Signature":  sig,
                    "Content-Type":    "application/json",
                }
                r = await hc.get(f"{CLOB_BASE}{path}", headers=headers)
            if r.status_code == 200:
                orders = r.json()
                if isinstance(orders, list):
                    print(f"  ✅ Open orders   : {len(orders)}")
                else:
                    print(f"  ✅ Open orders   : {orders}")
            else:
                print(f"  ⚠️  Open orders HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"  ⚠️  Open orders error: {e}")

        # Positions / trades on CLOB
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
                path = "/trades"
                ts   = str(int(time.time() * 1000))
                _tmp2 = PolymarketTradingClient()
                sig   = _tmp2._sign(ts, "GET", path)
                headers = {
                    "X-PM-Access-Key": settings.polymarket.api_key,
                    "X-PM-Timestamp":  ts,
                    "X-PM-Signature":  sig,
                    "Content-Type":    "application/json",
                }
                r = await hc.get(f"{CLOB_BASE}{path}", headers=headers,
                                  params={"limit": 5})
            if r.status_code == 200:
                trades = r.json()
                items  = trades if isinstance(trades, list) else trades.get("data", [])
                print(f"  ✅ Recent trades : {len(items)}")
                for t in items[:3]:
                    print(f"    {t.get('timestamp','?')[:16]}  {t.get('side','?')}  "
                          f"${t.get('size','?')}  @ {t.get('price','?')}")
            else:
                print(f"  ⚠️  Trades HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"  ⚠️  Trades error: {e}")

    print("\n" + "=" * 60)
    await c.close()


asyncio.run(main())
