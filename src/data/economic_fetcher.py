"""
Economic indicator data — free public sources, no API key required.

Sources:
  - US Bureau of Labor Statistics (BLS) public data API — CPI, unemployment
  - US Treasury — Fed funds rate, yield curve
  - FRED (St. Louis Fed) — public data series

Covers Kalshi markets like:
  "Will CPI exceed 3.5% in June?"
  "Will unemployment stay below 4%?"
  "Will the Fed raise rates in July?"
  "Will the 10-year yield exceed 4.5%?"
"""

import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.economic_fetcher")

_TIMEOUT = httpx.Timeout(10.0)

# BLS public series IDs (no API key — limited to 25 req/day but plenty for trading)
_BLS_SERIES: Dict[str, str] = {
    "cpi":          "CUUR0000SA0",   # CPI All Urban Consumers
    "cpi_core":     "CUUR0000SA0L1E", # CPI Core (ex food & energy)
    "unemployment": "LNS14000000",   # Unemployment rate
    "nonfarm":      "CES0000000001", # Nonfarm payrolls
}

# Economic keywords → which indicators to fetch
_INDICATOR_PATTERNS: Dict[str, List[str]] = {
    "cpi":          ["cpi", "inflation", "consumer price", "price index"],
    "unemployment": ["unemployment", "jobless", "jobs report", "nonfarm", "payroll"],
    "fed":          ["fed", "federal reserve", "fomc", "interest rate", "rate hike", "rate cut"],
    "gdp":          ["gdp", "gross domestic", "economic growth", "recession"],
    "yield":        ["10-year", "10 year", "treasury yield", "bond yield", "yield curve"],
}


def detect_economic_topics(title: str) -> List[str]:
    """Return list of economic topics relevant to the market title."""
    t = title.lower()
    found = []
    for topic, patterns in _INDICATOR_PATTERNS.items():
        if any(p in t for p in patterns):
            found.append(topic)
    return found


async def fetch_bls_series(series_id: str) -> Optional[Dict]:
    """Fetch last 3 data points from a BLS series (public API, no key)."""
    url = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
    payload = {"seriesid": [series_id], "latest": True}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        series_data = data.get("Results", {}).get("series", [{}])[0]
        points = series_data.get("data", [])[:3]
        return {"series_id": series_id, "points": points}
    except Exception as e:
        logger.warning("BLS fetch failed for %s: %s", series_id, e)
        return None


async def fetch_treasury_rates() -> Optional[Dict]:
    """
    Fetch current US Treasury yield curve from Treasury.gov.
    Returns rates for key maturities.
    """
    url = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/2024/all?type=daily_treasury_yield_curve&field_tdr_date_value=2024&download=true"
    # Use FRED instead — simpler JSON
    fred_url = "https://fred.stlouisfed.org/graph/fredgraph.json?id=DFF"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(fred_url)
            r.raise_for_status()
            data = r.json()
        observations = data.get("observations", [])
        if observations:
            latest = observations[-1]
            return {
                "fed_funds_rate": latest.get("value"),
                "date":           latest.get("date"),
            }
        return None
    except Exception as e:
        logger.warning("Fed funds rate fetch failed: %s", e)
        return None


async def fetch_10y_yield() -> Optional[Dict]:
    """Fetch the current 10-year Treasury yield from FRED."""
    fred_url = "https://fred.stlouisfed.org/graph/fredgraph.json?id=DGS10"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(fred_url)
            r.raise_for_status()
            data = r.json()
        observations = [o for o in data.get("observations", []) if o.get("value") != "."]
        if observations:
            latest = observations[-1]
            prev   = observations[-2] if len(observations) > 1 else latest
            val    = float(latest["value"])
            prev_v = float(prev["value"])
            return {
                "yield_10y":    val,
                "prev_10y":     prev_v,
                "change_10y":   val - prev_v,
                "date":         latest["date"],
            }
        return None
    except Exception as e:
        logger.warning("10y yield fetch failed: %s", e)
        return None


async def fetch_economic_context(title: str) -> Optional[str]:
    """
    High-level: detect what economic topics the market is about,
    fetch relevant data, return a formatted context string.
    """
    topics = detect_economic_topics(title)
    if not topics:
        return None

    import asyncio
    lines = ["Economic indicators:"]
    tasks = {}

    if "fed" in topics or "yield" in topics:
        tasks["fed"]  = fetch_treasury_rates()
        tasks["10y"]  = fetch_10y_yield()
    if "cpi" in topics:
        tasks["cpi"]  = fetch_bls_series(_BLS_SERIES["cpi"])
    if "unemployment" in topics:
        tasks["unemp"] = fetch_bls_series(_BLS_SERIES["unemployment"])

    if not tasks:
        return None

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    result_map = dict(zip(tasks.keys(), results))

    fed = result_map.get("fed")
    if isinstance(fed, dict) and fed:
        lines.append(f"  Fed Funds Rate: {fed['fed_funds_rate']}%  (as of {fed['date']})")

    y10 = result_map.get("10y")
    if isinstance(y10, dict) and y10:
        chg = y10["change_10y"]
        chg_str = f"{chg:+.3f}%" if chg else ""
        lines.append(
            f"  10-Year Treasury Yield: {y10['yield_10y']:.3f}%  {chg_str}  (as of {y10['date']})"
        )

    cpi = result_map.get("cpi")
    if isinstance(cpi, dict) and cpi:
        pts = cpi.get("points", [])
        if pts:
            p = pts[0]
            lines.append(f"  CPI (All Urban): {p.get('value')}  ({p.get('periodName')} {p.get('year')})")

    unemp = result_map.get("unemp")
    if isinstance(unemp, dict) and unemp:
        pts = unemp.get("points", [])
        if pts:
            p = pts[0]
            lines.append(f"  Unemployment Rate: {p.get('value')}%  ({p.get('periodName')} {p.get('year')})")

    return "\n".join(lines) if len(lines) > 1 else None
