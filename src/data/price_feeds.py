"""
Real-time asset price feeds — free, no API key required.

Sources:
  - CoinGecko  : crypto spot prices + 7-day change
  - Yahoo Finance (unofficial JSON) : stocks, indices, forex
  - Federal Reserve FRED (public) : macro indicators

All return normalised dicts so context_builder doesn't care which source ran.
"""

import logging
import asyncio
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.price_feeds")

_TIMEOUT = httpx.Timeout(10.0)

# CoinGecko coin IDs for common Kalshi crypto markets
CRYPTO_IDS: Dict[str, str] = {
    "btc":      "bitcoin",
    "bitcoin":  "bitcoin",
    "eth":      "ethereum",
    "ethereum": "ethereum",
    "sol":      "solana",
    "solana":   "solana",
    "xrp":      "ripple",
    "bnb":      "binancecoin",
    "doge":     "dogecoin",
    "ada":      "cardano",
    "avax":     "avalanche-2",
    "link":     "chainlink",
    "matic":    "matic-network",
}

# Yahoo Finance symbols for common Kalshi financial markets
EQUITY_SYMBOLS: Dict[str, str] = {
    "sp500":   "^GSPC",
    "s&p":     "^GSPC",
    "nasdaq":  "^IXIC",
    "dow":     "^DJI",
    "vix":     "^VIX",
    "oil":     "CL=F",
    "gold":    "GC=F",
    "silver":  "SI=F",
    "10y":     "^TNX",
    "treasury":"^TNX",
    "fed":     "^TNX",
    "eur":     "EURUSD=X",
    "gbp":     "GBPUSD=X",
}


async def get_crypto_price(coin_keyword: str) -> Optional[Dict]:
    """
    Fetch crypto price + 24h/7d change from CoinGecko.
    Returns dict or None on failure.
    """
    cid = CRYPTO_IDS.get(coin_keyword.lower())
    if not cid:
        return None
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={cid}&vs_currencies=usd"
        f"&include_24hr_change=true&include_7d_change=true"
    )
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json().get(cid, {})
                price = data.get("usd")
                if price is None:
                    continue
                return {
                    "asset":      coin_keyword.upper(),
                    "price_usd":  price,
                    "change_24h": data.get("usd_24h_change"),
                    "change_7d":  data.get("usd_7d_change"),
                    "source":     "coingecko",
                }
        except Exception as e:
            logger.debug("CoinGecko fetch failed for %s (attempt %d): %s", coin_keyword, attempt + 1, e)
            if attempt == 0:
                await asyncio.sleep(1)
    return None


async def get_equity_price(symbol_keyword: str) -> Optional[Dict]:
    """
    Fetch equity / index / commodity price from Yahoo Finance.
    Returns dict or None on failure.
    """
    symbol = EQUITY_SYMBOLS.get(symbol_keyword.lower())
    if not symbol:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            r = await client.get(url)
            r.raise_for_status()
            meta = r.json()["chart"]["result"][0]["meta"]
            price      = meta.get("regularMarketPrice")
            prev_close = meta.get("previousClose") or meta.get("chartPreviousClose")
            chg_pct    = ((price - prev_close) / prev_close * 100) if prev_close else None
            return {
                "asset":      symbol_keyword.upper(),
                "price":      price,
                "prev_close": prev_close,
                "change_pct": chg_pct,
                "currency":   meta.get("currency", "USD"),
                "source":     "yahoo_finance",
            }
    except Exception as e:
        logger.debug("Yahoo Finance fetch failed for %s: %s", symbol_keyword, e)
        return None


async def get_prices_for_keywords(keywords: List[str]) -> List[Dict]:
    """
    Given a list of keywords extracted from a market title, fetch all
    relevant price data in parallel.
    """
    tasks = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in CRYPTO_IDS:
            tasks.append(get_crypto_price(kw_lower))
        if kw_lower in EQUITY_SYMBOLS:
            tasks.append(get_equity_price(kw_lower))

    if not tasks:
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


def format_prices(prices: List[Dict]) -> str:
    """Format price data as a clean string for the AI prompt."""
    if not prices:
        return ""
    lines = ["Current market prices:"]
    for p in prices:
        if p.get("price_usd") is not None:
            chg24 = p.get("change_24h")
            chg7  = p.get("change_7d")
            chg_str = ""
            if chg24 is not None:
                chg_str += f"  24h: {chg24:+.1f}%"
            if chg7 is not None:
                chg_str += f"  7d: {chg7:+.1f}%"
            lines.append(f"  {p['asset']}: ${p['price_usd']:,.2f}{chg_str}")
        elif p.get("price") is not None:
            chg = p.get("change_pct")
            chg_str = f"  1d: {chg:+.1f}%" if chg is not None else ""
            lines.append(f"  {p['asset']}: {p['price']:,.2f} {p.get('currency','')}{chg_str}")
    return "\n".join(lines)
