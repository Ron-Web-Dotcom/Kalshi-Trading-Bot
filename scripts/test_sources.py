"""
Run this on your server to test every data source endpoint.
  python3 scripts/test_sources.py

Prints PASS/FAIL for each source and removes failing ones from web_search.py
if you run with --fix flag.
"""

import asyncio
import sys
import httpx

TIMEOUT = httpx.Timeout(10.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

TESTS = [
    # (name, url, as_json, remove_key)   remove_key = function name to strip if FAIL
    ("Google News RSS",  "https://news.google.com/rss/search?q=bitcoin&hl=en-US&gl=US&ceid=US:en", False, "_google_news"),
    ("Yahoo News RSS",   "https://news.yahoo.com/rss/search?p=bitcoin",                             False, "_yahoo_news"),
    ("Bing News RSS",    "https://www.bing.com/news/search?q=bitcoin&format=rss",                   False, "_bing_news"),
    ("Guardian RSS",     "https://www.theguardian.com/search?q=bitcoin&format=rss",                 False, "_guardian_news"),
    ("AlJazeera RSS",    "https://www.aljazeera.com/search/bitcoin?format=rss",                     False, "_aljazeera_news"),
    ("NPR RSS",          "https://feeds.npr.org/1001/rss.xml",                                      False, "_npr_news"),
    ("AP News RSS",      "https://feeds.apnews.com/rss/apf-topnews",                                False, "_ap_news"),
    ("Reuters RSS",      "https://feeds.reuters.com/reuters/topNews",                               False, "_reuters_news"),
    ("BBC RSS",          "https://feeds.bbci.co.uk/news/rss.xml",                                   False, "_bbc_news"),
    ("Manifold",         "https://api.manifold.markets/v0/search-markets?term=bitcoin&limit=2",      True,  "_manifold_markets"),
    ("Metaculus",        "https://www.metaculus.com/api2/questions/?search=bitcoin&limit=2",         True,  "_metaculus"),
    ("Polymarket",       "https://gamma-api.polymarket.com/markets?search=bitcoin&active=true&limit=3", True, "_polymarket_price"),
    ("PredictIt",        "https://www.predictit.org/api/marketdata/all/",                           True,  "_predictit_price"),
    ("Wikipedia",        "https://en.wikipedia.org/api/rest_v1/page/summary/Bitcoin",               True,  "_wikipedia"),
    ("DuckDuckGo",       "https://api.duckduckgo.com/?q=bitcoin&format=json&no_html=1&skip_disambig=1", True, "_ddg_instant"),
    ("Wikidata",         "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=bitcoin&language=en&limit=2&format=json", True, "_wikidata"),
    ("Reddit",           "https://www.reddit.com/search.json?q=bitcoin&sort=new&limit=3&t=week",    True,  "_reddit_search"),
    ("YouTube",          "https://www.youtube.com/results?search_query=bitcoin",                    False, "_youtube_search"),
    ("CoinGecko",        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", True, None),
    ("Yahoo Finance",    "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&range=1d", True, None),
    ("ESPN scoreboard",  "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard", True,  None),
    ("FRED (fed funds)", "https://fred.stlouisfed.org/graph/fredgraph.csv?id=FEDFUNDS",             False, None),
    ("wttr.in weather",  "https://wttr.in/New+York?format=j1",                                      True,  None),
]


async def test_one(name, url, as_json):
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            body = r.json() if as_json else r.text
            size = len(str(body))
            print(f"  ✅ PASS  {name:<25} ({size:,} bytes)")
            return True
    except Exception as e:
        print(f"  ❌ FAIL  {name:<25} {e}")
        return False


async def main():
    fix = "--fix" in sys.argv
    print(f"\nTesting {len(TESTS)} data sources...\n")

    results = await asyncio.gather(*[test_one(n, u, j) for n, u, j, _ in TESTS])

    passed = sum(results)
    failed = [(TESTS[i][0], TESTS[i][3]) for i, ok in enumerate(results) if not ok]

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{len(TESTS)} PASS  |  {len(failed)} FAIL")

    if failed:
        print(f"\nFailed sources:")
        for name, key in failed:
            tag = f" (remove_key={key})" if key else " (domain-specific fetcher — check separately)"
            print(f"  ❌ {name}{tag}")

    if fix and failed:
        removable = [(n, k) for n, k in failed if k]
        if removable:
            print(f"\n--fix: would remove {len(removable)} sources from web_search.py")
            print("Run without --fix first to review, then apply manually.")
        else:
            print("\n--fix: no web_search.py sources to auto-remove (domain fetchers need manual review)")

    if passed == len(TESTS):
        print("\n✅ All sources healthy — bot has maximum context for every evaluation.")
    else:
        print(f"\n⚠️  {len(failed)} sources failing — check connectivity or API availability.")


if __name__ == "__main__":
    asyncio.run(main())
