"""
Polymarket full API diagnostic — run on VPS:
    python3 test_poly.py

Checks:
  1. Gamma API  — market data (public, no auth)
  2. CLOB API   — balance, open orders, trades (requires key + secret + passphrase)

Auth scheme: HMAC-SHA256 L2
  Headers: POLY-ACCESS-TOKEN, POLY-TIMESTAMP, POLY-SIGNATURE, POLY-PASSPHRASE
  Required .env vars:
    POLY_API_KEY        = <uuid from Polymarket API key page>
    POLY_API_SECRET     = <base64-encoded secret>
    POLY_API_PASSPHRASE = <passphrase set when creating the key>
    POLY_WALLET_ADDRESS = <0x... Polygon wallet>
"""
import asyncio
import hmac
import hashlib
import base64
import sys
import time
sys.path.insert(0, "/root/trading-bot")


async def main():
    import httpx
    from src.clients.polymarket_client import PolymarketTradingClient, GAMMA_BASE, CLOB_BASE
    from src.config.settings import settings
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    c      = PolymarketTradingClient()
    now_et = datetime.now(_ET)

    print("=" * 60)
    print("  POLYMARKET API DIAGNOSTIC")
    print(f"  {now_et.strftime('%A %B %d %Y %I:%M %p ET')}")
    print("=" * 60)

    # ── Config ───────────────────────────────────────────────────────────────
    print("\n── Config ──────────────────────────────────────────────────")
    print(f"  POLY_API_KEY set        : {bool(settings.polymarket.api_key)}")
    print(f"  POLY_API_SECRET set     : {bool(settings.polymarket.api_secret)}")
    print(f"  POLY_API_PASSPHRASE set : {bool(settings.polymarket.api_passphrase)}")
    print(f"  POLY_WALLET_ADDRESS     : {settings.polymarket.wallet_address or '❌ NOT SET'}")
    print(f"  POLY_ENABLED            : {settings.polymarket.enabled}")
    print(f"  POLY_LIVE_TRADING       : {settings.polymarket.live_trading_enabled}")

    missing = []
    if not settings.polymarket.api_key:
        missing.append("POLY_API_KEY")
    if not settings.polymarket.api_secret:
        missing.append("POLY_API_SECRET")
    if not settings.polymarket.api_passphrase:
        missing.append("POLY_API_PASSPHRASE  ← needed for CLOB auth")
    if not settings.polymarket.wallet_address:
        missing.append("POLY_WALLET_ADDRESS  ← needed for live orders")
    if missing:
        print(f"\n  ⚠️  Missing .env vars: {', '.join(missing)}")

    # ── Gamma API — market data ───────────────────────────────────────────────
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

    # Live now (≤2h)
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

    # ── CLOB API — authenticated ──────────────────────────────────────────────
    print("\n── CLOB API (authenticated, HMAC-SHA256 L2) ─────────────────")

    if not settings.polymarket.api_key or not settings.polymarket.api_secret:
        print("  ❌ POLY_API_KEY or POLY_API_SECRET missing — skipping CLOB checks")
        print("\n" + "=" * 60)
        await c.close()
        return

    def _make_sig(ts: str, method: str, path: str, body: str = "") -> str:
        secret_raw = settings.polymarket.api_secret
        try:
            secret = base64.b64decode(secret_raw)
        except Exception:
            secret = secret_raw.encode()
        msg = (ts + method.upper() + path + body).encode()
        return base64.b64encode(hmac.new(secret, msg, hashlib.sha256).digest()).decode()

    def _auth(method: str, path: str, body: str = "") -> dict:
        ts  = str(int(time.time()))   # seconds
        sig = _make_sig(ts, method, path, body)
        h = {
            "POLY-ACCESS-TOKEN": settings.polymarket.api_key,
            "POLY-TIMESTAMP":    ts,
            "POLY-SIGNATURE":    sig,
            "Content-Type":      "application/json",
        }
        if settings.polymarket.api_passphrase:
            h["POLY-PASSPHRASE"] = settings.polymarket.api_passphrase
        return h

    # Balance
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
            r = await hc.get(f"{CLOB_BASE}/balance", headers=_auth("GET", "/balance"))
        if r.status_code == 200:
            bal = float(r.json().get("balance", 0))
            print(f"  ✅ USDC Balance  : ${bal:.2f}")
        else:
            print(f"  ❌ Balance HTTP {r.status_code}: {r.text[:150]}")
            if r.status_code == 401:
                print("     → Check POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE")
    except Exception as e:
        print(f"  ❌ Balance error: {e}")

    # Open orders
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
            r = await hc.get(f"{CLOB_BASE}/orders", headers=_auth("GET", "/orders"))
        if r.status_code == 200:
            orders = r.json()
            items  = orders if isinstance(orders, list) else orders.get("data", [])
            print(f"  ✅ Open orders   : {len(items)}")
        else:
            print(f"  ⚠️  Open orders HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠️  Open orders error: {e}")

    # Recent trades
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
            r = await hc.get(f"{CLOB_BASE}/trades", params={"limit": 5}, headers=_auth("GET", "/trades"))
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

    # CLOB simplified markets (token IDs)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False) as hc:
            r = await hc.get(f"{CLOB_BASE}/markets", params={"limit": 5})
        if r.status_code == 200:
            raw   = r.json()
            items = raw if isinstance(raw, list) else raw.get("data", [])
            print(f"  ✅ CLOB markets  : {len(items)} returned (first 5 sample)")
        else:
            print(f"  ⚠️  CLOB markets HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"  ⚠️  CLOB markets error: {e}")

    print("\n" + "=" * 60)
    await c.close()


asyncio.run(main())
