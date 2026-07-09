#!/usr/bin/env python3
"""
On-demand data source health check.
Run this after fixing a failed source to re-enable it immediately.

Usage:
  python3 scripts/health_check.py          # check all, update DISABLED_SOURCES, Discord alert
  python3 scripts/health_check.py --silent  # check only, no Discord alert

What it does:
  1. Probes all 18 active sources
  2. Removes passing sources from DISABLED_SOURCES (re-enables them for the bot)
  3. Adds failing sources to DISABLED_SOURCES (bot skips them)
  4. Sends a Discord summary of what changed
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except Exception:
    pass

HEALTH_SOURCES = [
    ("google_news",   "https://news.google.com/rss/search?q=bitcoin&hl=en-US&gl=US&ceid=US:en", False),
    ("yahoo_news",    "https://news.yahoo.com/rss/search?p=bitcoin",                             False),
    ("bing_news",     "https://www.bing.com/news/search?q=bitcoin&format=rss",                   False),
    ("aljazeera",     "https://www.aljazeera.com/search/bitcoin?format=rss",                     False),
    ("npr",           "https://feeds.npr.org/1001/rss.xml",                                      False),
    ("bbc",           "https://feeds.bbci.co.uk/news/rss.xml",                                   False),
    ("manifold",      "https://api.manifold.markets/v0/search-markets?term=bitcoin&limit=2",     True),
    ("polymarket",    "https://gamma-api.polymarket.com/markets?search=bitcoin&active=true&limit=3", True),
    ("predictit",     "https://www.predictit.org/api/marketdata/all/",                           True),
    ("wikipedia",     "https://en.wikipedia.org/api/rest_v1/page/summary/Bitcoin",               True),
    ("duckduckgo",    "https://api.duckduckgo.com/?q=bitcoin&format=json&no_html=1&skip_disambig=1", True),
    ("wikidata",      "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=bitcoin&language=en&limit=2&format=json", True),
    ("youtube",       "https://www.youtube.com/results?search_query=bitcoin",                    False),
    ("coingecko",     "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", True),
    ("yahoo_finance", "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&range=1d", True),
    ("espn",          "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", True),
    ("fred",          "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",             False),
    ("wttr",          "https://wttr.in/New+York?format=j1",                                      True),
]

G  = "\033[92m"
R  = "\033[91m"
W  = "\033[93m"
NC = "\033[0m"


async def probe(name: str, url: str, as_json: bool) -> bool:
    import httpx
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=10, headers=ua, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            _ = r.json() if as_json else r.text
            return True
    except Exception as e:
        print(f"  {R}❌ FAIL{NC}  {name:<20} {str(e)[:70]}")
        return False


async def main():
    silent = "--silent" in sys.argv

    print(f"\n{'='*55}")
    print(f"  Data Source Health Check — {len(HEALTH_SOURCES)} sources")
    print(f"{'='*55}\n")

    results = await asyncio.gather(*[probe(n, u, j) for n, u, j in HEALTH_SOURCES])

    # Load current disabled set from web_search module
    try:
        from src.data.web_search import DISABLED_SOURCES
    except Exception as e:
        print(f"  {W}⚠️  Could not import DISABLED_SOURCES: {e}{NC}")
        print(f"  Results only — no sources will be toggled in the running bot.")
        DISABLED_SOURCES = set()
        silent = True

    newly_disabled = []
    newly_enabled  = []
    still_disabled = []
    passing        = []

    for (name, _, _), ok in zip(HEALTH_SOURCES, results):
        if ok:
            if name in DISABLED_SOURCES:
                DISABLED_SOURCES.discard(name)
                newly_enabled.append(name)
                print(f"  {G}✅ RE-ENABLED{NC}  {name}")
            else:
                passing.append(name)
                print(f"  {G}✅ PASS{NC}       {name}")
        else:
            if name not in DISABLED_SOURCES:
                DISABLED_SOURCES.add(name)
                newly_disabled.append(name)
            else:
                still_disabled.append(name)

    passed = sum(results)
    failed = len(HEALTH_SOURCES) - passed

    print(f"\n{'='*55}")
    print(f"  {passed}/{len(HEALTH_SOURCES)} healthy   {failed} failing")
    if newly_enabled:
        print(f"  {G}Re-enabled:{NC}  {', '.join(newly_enabled)}")
    if newly_disabled:
        print(f"  {R}Newly disabled:{NC} {', '.join(newly_disabled)}")
    if still_disabled:
        print(f"  {W}Still disabled:{NC} {', '.join(still_disabled)}")
    print(f"{'='*55}\n")

    # Discord notification
    if not silent:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if webhook:
            import httpx
            lines = [f"**Source Health Check — {passed}/{len(HEALTH_SOURCES)} healthy**\n"]
            if newly_enabled:
                lines.append(f"✅ **Re-enabled** ({len(newly_enabled)}): {', '.join(newly_enabled)}")
            if newly_disabled:
                lines.append(f"🚫 **Newly disabled** ({len(newly_disabled)}): {', '.join(newly_disabled)}")
            if still_disabled:
                lines.append(f"⛔ **Still disabled** ({len(still_disabled)}): {', '.join(still_disabled)}")
            if failed == 0:
                lines.append("✅ All sources healthy.")
            try:
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.post(webhook, json={"content": "\n".join(lines)})
                print("  Discord alert sent.")
            except Exception as e:
                print(f"  Discord alert failed: {e}")
        else:
            print("  (DISCORD_WEBHOOK_URL not set — skipping alert)")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
