"""Run on VPS: python3 test_poly.py"""
import asyncio
import sys
sys.path.insert(0, "/root/trading-bot")

async def main():
    from src.clients.polymarket_client import PolymarketTradingClient
    from src.config.settings import settings
    from datetime import datetime
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")

    c = PolymarketTradingClient()

    print("=== Polymarket Config ===")
    print(f"POLY_API_KEY set : {bool(settings.polymarket.api_key)}")
    print(f"POLY_ENABLED     : {settings.polymarket.enabled}")
    print(f"POLY_LIVE_TRADING: {settings.polymarket.live_trading_enabled}")
    print()

    print("=== Balance / Deposit ===")
    print(f"POLY_API_SECRET set: {bool(settings.polymarket.api_secret)}")
    bal = await c.get_balance()
    if bal is not None:
        print(f"✅  USDC Balance: ${bal:.2f}")
    else:
        if not settings.polymarket.api_secret:
            print("❌  POLY_API_SECRET is missing from .env — add it to authenticate")
        else:
            print("❌  Balance returned None — API key/secret may be wrong or wallet not linked")
    print()

    print("=== Live Markets (today ET only) ===")
    now_et = datetime.now(_ET)
    today  = now_et.strftime("%Y-%m-%d")
    try:
        markets = await c.get_markets(limit=20)
        today_markets = [
            m for m in markets
            if (m.get("close_time") or "").startswith(today)
        ]
        print(f"Total fetched : {len(markets)}")
        print(f"Closing today : {len(today_markets)}")
        print()
        for m in today_markets[:10]:
            print(
                f"  {m.get('close_time','?')[:16]}  "
                f"YES={m.get('yes_ask',0):>3.0f}¢  NO={m.get('no_ask',0):>3.0f}¢  "
                f"vol={m.get('volume',0):>6,.0f}  "
                f"{m.get('title','?')[:55]}"
            )
    except Exception as e:
        print(f"❌  Market fetch error: {e}")
    print()

    print("=== Live Right Now (closing within 2h, every category) ===")
    try:
        live = await c.get_live_now_markets(max_markets=500)
        print(f"Live now (all categories): {len(live)}")
        for m in live[:10]:
            print(
                f"  {m.get('close_time','?')[:16]}  "
                f"YES={m.get('yes_ask',0):>3.0f}¢  "
                f"{m.get('title','?')[:55]}"
            )
    except Exception as e:
        print(f"Live check error: {e}")

    await c.close()

asyncio.run(main())
