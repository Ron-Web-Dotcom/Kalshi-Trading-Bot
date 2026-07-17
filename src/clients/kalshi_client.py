"""Kalshi API v2 client — RSA-signed auth with simple API key fallback."""

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

# Global rate limiter shared across ALL KalshiClient instances
# Kalshi allows ~3 req/s sustained — use a semaphore to serialize across tasks
_KALSHI_GLOBAL_LOCK: Optional[asyncio.Lock] = None

def _get_global_lock() -> asyncio.Lock:
    global _KALSHI_GLOBAL_LOCK
    if _KALSHI_GLOBAL_LOCK is None:
        _KALSHI_GLOBAL_LOCK = asyncio.Lock()
    return _KALSHI_GLOBAL_LOCK
_KALSHI_MIN_INTERVAL = 0.4   # 2.5 req/s — safely under Kalshi's limit
_kalshi_last_request = 0.0

logger = logging.getLogger("trading.kalshi_client")


def _load_private_key(pem_text: str):
    """Load RSA private key from PEM string for request signing."""
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(pem_text.encode(), password=None)
    return key


class KalshiClient:
    """
    Async Kalshi API v2 client.

    Auth priority:
      1. RSA private key (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PEM/PATH)
      2. Simple API key header fallback (KALSHI_API_KEY)
    """

    def __init__(self):
        from src.config.settings import settings
        self.cfg = settings.kalshi
        self._client: Optional[httpx.AsyncClient] = None
        self._private_key = None
        self._last_request_time = 0.0
        self._min_interval = 1.0 / max(self.cfg.rate_limit_per_second, 1)

        if self.cfg.api_key_id and (self.cfg.private_key_pem or self.cfg.private_key_path):
            try:
                pem = self.cfg.private_key_pem
                if not pem and self.cfg.private_key_path:
                    with open(self.cfg.private_key_path) as f:
                        pem = f.read()
                self._private_key = _load_private_key(pem)
                logger.info("Kalshi auth: RSA key loaded")
            except Exception as e:
                logger.warning(f"RSA key load failed ({e}), falling back to API key auth")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.cfg.base_url,
                timeout=self.cfg.timeout,
            )
        return self._client

    def _sign_request(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from urllib.parse import urlparse
        ts = str(int(time.time() * 1000))
        # Kalshi expects full path in signature (e.g. /trade-api/v2/markets)
        base_path = urlparse(self.cfg.base_url).path.rstrip("/")
        full_path = base_path + path if not path.startswith(base_path) else path
        msg = ts + method.upper() + full_path
        sig = self._private_key.sign(msg.encode(), padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.cfg.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _build_headers(self, method: str = "GET", path: str = "/", body: str = "") -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._private_key and self.cfg.api_key_id:
            headers.update(self._sign_request(method, path, body))
        elif self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    async def _rate_limit(self):
        global _kalshi_last_request
        async with _get_global_lock():
            elapsed = time.monotonic() - _kalshi_last_request
            if elapsed < _KALSHI_MIN_INTERVAL:
                await asyncio.sleep(_KALSHI_MIN_INTERVAL - elapsed)
            _kalshi_last_request = time.monotonic()

    async def _request(self, method: str, path: str, params: Optional[Dict] = None,
                       body: Optional[Dict] = None, retries: int = 3) -> Any:
        await self._rate_limit()
        client = await self._get_client()
        body_str = json.dumps(body) if body else ""

        for attempt in range(retries):
            try:
                # Kalshi v2 RSA signature uses path only — no query string
                # Headers must be rebuilt each attempt so the RSA timestamp is fresh
                headers = self._build_headers(method, path, body_str)
                resp = await client.request(
                    method, path, params=params,
                    content=body_str.encode() if body_str else None,
                    headers=headers
                )
                if resp.status_code == 429:
                    wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    logger.warning(f"Rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    logger.error("Kalshi 401 Unauthorized on %s %s — %s", method, path, e.response.text[:100])
                    raise
                if attempt == retries - 1:
                    _safe = e.response.text[:200]
                    if self.cfg.api_key_id:
                        _safe = _safe.replace(self.cfg.api_key_id, "[KEY_ID]")
                    logger.error("HTTP %d on %s %s: %s", e.response.status_code, method, path, _safe)
                    raise
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"Request failed {method} {path}: {e}")
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"_request exhausted all {retries} retries due to rate limiting on {method} {path}")

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 200, cursor: str = "", status: str = "open",
                          sort_by: str = "", order: str = "") -> Dict:
        params: Dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if sort_by:
            params["sort_by"] = sort_by
        if order:
            params["order"] = order
        return await self._request("GET", "/markets", params=params)

    async def get_live_now_markets(self, max_markets: int = 200) -> List[Dict]:
        """
        Fetch markets that are ACTUALLY LIVE RIGHT NOW using Kalshi's live event API.

        Kalshi shows "LIVE 38" in the nav — these are real in-progress events
        (Czechia vs Guatemala 74', NBA game Q3, etc.), not just closing-soon markets.

        Uses the /events endpoint with status=active which returns events where
        a live game/match is in progress. Falls back to /markets with live-specific
        category tags if events endpoint unavailable.
        """
        live_markets: List[Dict] = []
        now = datetime.now(_ET)

        # Strategy 1: /events endpoint — grab all open sport events closing within 24h
        # Kalshi doesn't expose an "in_game" flag, so we rely on:
        #   a) category == "Sports" AND closes within 24h (game is today)
        #   b) title contains "vs" / game keywords (individual match, not tournament winner)
        _sport_categories = {"sports", "sport", "soccer", "football", "baseball", "basketball",
                             "hockey", "tennis", "golf", "ufc", "mma", "boxing", "racing",
                             "cricket", "rugby", "esports"}
        _game_title_kws   = ["vs ", " vs ", " vs.", "game ", "match ", "inning", "quarter",
                             "half", "period", "set ", "overtime", "bout", "race "]
        try:
            data = await self._request("GET", "/events", params={"status": "open", "limit": 200})
            events = data.get("events") or []
            for event in events:
                category = (event.get("category") or "").lower()
                title    = event.get("title") or event.get("event_title") or ""
                if not title:
                    continue
                title_l = title.lower()
                is_sport    = category in _sport_categories
                is_game     = any(kw in title_l for kw in _game_title_kws)
                # Accept: sport category OR game keywords in title
                if not (is_sport or is_game):
                    continue
                # Check close_time on the event or its markets
                event_close = event.get("close_time") or event.get("end_date") or ""
                hours_away  = None
                if event_close:
                    try:
                        cdt = datetime.fromisoformat(str(event_close).replace("Z", "+00:00"))
                        if cdt.tzinfo is None:
                            cdt = cdt.replace(tzinfo=timezone.utc).astimezone(_ET)
                        hours_away = (cdt - now).total_seconds() / 3600
                    except Exception:
                        pass
                for m in (event.get("markets") or []):
                    if not isinstance(m, dict):
                        continue
                    # Use market close_time if event didn't have one
                    m_close = m.get("close_time") or event_close
                    market_hours_away = hours_away
                    if m_close and market_hours_away is None:
                        try:
                            cdt = datetime.fromisoformat(str(m_close).replace("Z", "+00:00"))
                            if cdt.tzinfo is None:
                                cdt = cdt.replace(tzinfo=timezone.utc).astimezone(_ET)
                            market_hours_away = (cdt - now).total_seconds() / 3600
                        except Exception:
                            pass
                    if market_hours_away is not None and 0 < market_hours_away <= 24:
                        m.setdefault("title", title)
                        m.setdefault("category", category)
                        m.setdefault("close_time", m_close)
                        m["_kalshi_live"] = True
                        m["hours_to_close"] = round(market_hours_away, 2)
                        live_markets.append(m)
        except Exception as e:
            logger.debug("Kalshi /events live fetch: %s", e)

        if live_markets:
            # Normalize prices — events API uses yes_ask/no_ask as fractions (0-1) sometimes
            for m in live_markets:
                for field in ("yes_ask", "no_ask", "last_price", "yes_bid", "no_bid"):
                    v = m.get(field)
                    if v is not None:
                        try:
                            fv = float(v)
                            if 0 < fv < 1.0:  # fraction → cents
                                m[field] = round(fv * 100, 2)
                        except (TypeError, ValueError):
                            pass
            priced = sum(1 for m in live_markets if (m.get("yes_ask") or m.get("last_price") or 0) > 1)
            logger.info("Kalshi LIVE NOW (/events): %d sport/game markets closing ≤24h (%d with price)",
                        len(live_markets), priced)
            return live_markets[:max_markets]

        # Strategy 2: query each sport series directly — much faster than paginating all markets
        _live_event_kws = [
            "debate", "hearing", "press conference", "speech", "summit",
            "fed meeting", "fomc", "rate decision", "vote today", "voting",
            "live", "right now", "happening now", "in session",
            "hurricane", "tornado", "storm", "earthquake",
            "testimony", "trial", "verdict", "sentence",
            "inauguration", "swearing in", "signing",
            "launch", "landing", "spacewalk",
            "ipo today", "earnings today",
        ]
        _sport_title_kws = [
            "vs ", " vs", "match", "game", "quarter", "half", "period",
            "inning", "set ", "round", "bout", "race", "leg ",
            "moneyline", "spread", "over/under", "cover",
        ]
        live_series_prefixes = [
            "SOCCER", "FIFA", "NFL", "NBA", "MLB", "NHL", "UFC", "TENNIS",
            "F1", "GOLF", "RUGBY", "CRICKET", "BOXING", "WNBA", "MLS",
            "NCAAF", "NCAAB", "PGA", "NASCAR", "ESPORTS",
            "DEBATE", "HEARING", "FED", "ELECTION", "VOTE",
        ]
        try:
            markets = []
            # Fetch each sport series_ticker directly — avoids paginating 5000+ markets
            for prefix in live_series_prefixes:
                try:
                    data = await self._request("GET", "/markets", params={
                        "status": "open",
                        "series_ticker": prefix,
                        "limit": 100,
                    })
                    if not isinstance(data, dict):
                        continue
                    batch = data.get("markets") or []
                    markets.extend(batch)
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            # Also do one broad pass capped at 200 to catch any uncategorized markets
            try:
                data = await self.get_markets(limit=200, status="open")
                markets.extend(data.get("markets") or [])
            except Exception:
                pass

            now = datetime.now(_ET)
            for m in markets:
                ticker = (m.get("ticker") or "").upper()
                title  = (m.get("title") or "").lower()
                ct     = m.get("close_time") or ""
                try:
                    close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc).astimezone(_ET)
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if hours_left <= 0:
                        continue
                except Exception:
                    continue
                is_sport_ticker = any(ticker.startswith(p) for p in live_series_prefixes)
                is_sport_title  = any(kw in title for kw in _sport_title_kws)
                is_live_event   = any(kw in title for kw in _live_event_kws)
                # Sports: close within 24h — catches today's games day-of
                # Non-sport live events: close within 3h (time-bounded)
                if ((is_sport_ticker or is_sport_title) and hours_left <= 24) or (is_live_event and hours_left <= 3):
                    m["_kalshi_live"] = True
                    m["hours_to_close"] = round(hours_left, 2)
                    live_markets.append(m)

        except Exception as e:
            logger.debug("Kalshi live market scan: %s", e)

        # Normalize prices — Kalshi /markets API sometimes returns fractions
        for m in live_markets:
            for field in ("yes_ask", "no_ask", "last_price", "yes_bid", "no_bid"):
                v = m.get(field)
                if v is not None:
                    try:
                        fv = float(v)
                        if 0 < fv < 1.0:
                            m[field] = round(fv * 100, 2)
                    except (TypeError, ValueError):
                        pass
        priced = sum(1 for m in live_markets if (m.get("yes_ask") or m.get("last_price") or 0) > 1)
        logger.info("Kalshi LIVE NOW (fallback scan): %d confirmed live markets (%d with price)",
                    len(live_markets), priced)
        return live_markets[:max_markets]

    async def get_all_markets(self, status: str = "open", max_markets: int = 1000,
                               sort_by_close: bool = False) -> List[Dict]:
        """
        Fetch up to max_markets from Kalshi.
        sort_by_close=False (default): sort by volume desc — most liquid markets first.
        sort_by_close=True: sort by close_time asc — soonest-expiring markets first
                            (captures 1min/5min/1hr/daily short-duration markets).
        """
        markets = []
        cursor = ""
        while len(markets) < max_markets:
            data = await self.get_markets(limit=200, cursor=cursor, status=status)
            batch = data.get("markets", [])
            if not batch:
                break
            markets.extend(batch)
            cursor = data.get("cursor", "")
            if not cursor:
                break
            await asyncio.sleep(0.2)

        if sort_by_close:
            # Sort by close_time ascending — soonest closing first
            def _close_key(m):
                ct = m.get("close_time") or ""
                try:
                    dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc).astimezone(_ET)
                    return dt
                except Exception:
                    return datetime.max.replace(tzinfo=timezone.utc).astimezone(_ET)
            now = datetime.now(_ET)
            # Only include markets that haven't closed yet
            markets = [m for m in markets if _close_key(m) > now]
            markets.sort(key=_close_key)
        else:
            markets.sort(key=lambda m: m.get("volume", 0) or 0, reverse=True)

        return markets[:max_markets]

    async def get_live_markets(self, max_hours: float = 3.0, max_markets: int = 60,
                               db=None) -> List[Dict]:
        """
        Return Kalshi markets closing within max_hours.
        If db is provided, queries the DB (has enriched prices from ingest).
        Falls back to API with yes_ask→last_price→yes_bid price chain.
        """
        from datetime import datetime, timezone
        now = datetime.now(_ET)

        # ── DB path (preferred — prices already enriched by ingest job) ──────
        if db is not None:
            try:
                import datetime as _dt
                now_iso   = now.strftime("%Y-%m-%dT%H:%M:%S")
                max_iso   = (now + _dt.timedelta(hours=max_hours)).strftime("%Y-%m-%dT%H:%M:%S")
                rows = await db.fetchall(
                    """SELECT ticker, title, category, yes_ask, no_ask, yes_bid, no_bid,
                              last_price, volume, close_time, platform
                       FROM markets
                       WHERE (platform='kalshi' OR platform IS NULL)
                         AND (status='open' OR status='')
                         AND close_time > ? AND close_time <= ?
                       ORDER BY close_time ASC
                       LIMIT ?""",
                    (now_iso, max_iso, max_markets),
                )
                live = []
                def _norm(v):
                    try:
                        f = float(v or 0)
                        # Kalshi stores prices in cents (0-100); convert fractions to cents
                        return f * 100 if f < 1.0 else f
                    except Exception:
                        return 0.0
                for r in (rows or []):
                    m = dict(r)
                    yes_ask = _norm(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid"))
                    if not yes_ask:
                        continue  # skip truly priceless markets
                    no_ask = _norm(m.get("no_ask")) or round(100.0 - yes_ask, 2)
                    try:
                        close_dt = datetime.fromisoformat(str(m["close_time"]).replace("Z", "+00:00"))
                        if close_dt.tzinfo is None:
                            close_dt = close_dt.replace(tzinfo=timezone.utc).astimezone(_ET)
                        hours_left = (close_dt - now).total_seconds() / 3600
                    except Exception:
                        hours_left = 0
                    m.update(yes_ask=yes_ask, no_ask=no_ask, is_live=True,
                             hours_to_close=round(hours_left, 2), platform="kalshi")
                    live.append(m)
                logger.info("Kalshi live markets (DB, ≤%.0fh): %d of %d rows have price",
                            max_hours, len(live), len(rows or []))
                if live:
                    return live
                logger.warning("Kalshi DB: %d rows in window but 0 have price — check ingest", len(rows or []))
            except Exception as e:
                logger.warning("Kalshi live DB query failed, falling back to API: %s", e)

        # ── API fallback ──────────────────────────────────────────────────────
        try:
            markets = await self.get_all_markets(status="open", max_markets=500, sort_by_close=True)
            live, no_price = [], 0
            for m in markets:
                ct = m.get("close_time") or ""
                try:
                    close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc).astimezone(_ET)
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if not (0 < hours_left <= max_hours):
                        continue
                except Exception:
                    continue
                # Kalshi API prices can be in cents (int) or decimal — normalise both
                def _p(v):
                    try:
                        f = float(v or 0)
                        return f * 100 if f < 1.0 else f  # normalise fractions → cents
                    except Exception:
                        return 0.0
                yes_ask = _p(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid"))
                no_ask  = _p(m.get("no_ask")) or round(100.0 - yes_ask, 2)
                if yes_ask > 0:
                    m.update(yes_ask=yes_ask, no_ask=no_ask, is_live=True,
                             hours_to_close=round(hours_left, 2))
                    live.append(m)
                else:
                    no_price += 1
            logger.info("Kalshi live (API, ≤%.0fh): %d with price, %d no price", max_hours, len(live), no_price)
            return live[:max_markets]
        except Exception as e:
            logger.warning("Failed to fetch Kalshi live markets: %s", e)
            return []

    # ── Single market / event ─────────────────────────────────────────────────

    async def get_market(self, ticker: str) -> Dict:
        """Fetch a single market by ticker."""
        return await self._request("GET", f"/markets/{ticker}")

    async def get_market_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        """Full order book for a market — bids and asks with sizes."""
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def get_market_trades(self, ticker: str, limit: int = 50,
                                 cursor: str = "") -> Dict:
        """Recent trades for a specific market."""
        params: Dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets/trades", params=params)

    async def get_market_candlesticks(self, series_ticker: str, ticker: str,
                                      start_ts: int = 0, end_ts: int = 0,
                                      period_interval: int = 1) -> Dict:
        """
        OHLCV candlestick data for a market.
        period_interval: candle size in minutes (1, 5, 15, 60, 1440)
        start_ts / end_ts: unix timestamps (seconds)
        """
        params: Dict[str, Any] = {"period_interval": period_interval}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts
        return await self._request(
            "GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks", params=params
        )

    # ── Events ────────────────────────────────────────────────────────────────

    async def get_event(self, event_ticker: str) -> Dict:
        """Fetch a single event with all its markets."""
        return await self._request("GET", f"/events/{event_ticker}")

    async def get_events(self, limit: int = 200, cursor: str = "",
                          status: str = "open", series_ticker: str = "") -> Dict:
        """Fetch events list. status: open | closed | all."""
        params: Dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        if series_ticker:
            params["series_ticker"] = series_ticker
        return await self._request("GET", "/events", params=params)

    # ── Series ────────────────────────────────────────────────────────────────

    async def get_series(self, series_ticker: str) -> Dict:
        """Fetch metadata for a market series (e.g. NFL, NBA, BTC)."""
        return await self._request("GET", f"/series/{series_ticker}")

    # ── Portfolio — Balance & Positions ──────────────────────────────────────

    async def get_balance(self) -> Dict:
        """Account balance including available and total USDC."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, limit: int = 200, cursor: str = "",
                             ticker: str = "", event_ticker: str = "",
                             settlement_status: str = "") -> Dict:
        """
        Open positions. Filter by ticker, event, or settlement_status.
        settlement_status: all | unsettled | settled
        """
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if settlement_status:
            params["settlement_status"] = settlement_status
        return await self._request("GET", "/portfolio/positions", params=params)

    async def get_position(self, ticker: str) -> Dict:
        """Fetch position for a single market ticker."""
        return await self._request("GET", f"/portfolio/positions/{ticker}")

    # ── Portfolio — Orders ────────────────────────────────────────────────────

    async def get_orders(self, status: str = "", ticker: str = "",
                          event_ticker: str = "", limit: int = 200,
                          cursor: str = "") -> Dict:
        """
        Fetch orders. status: resting | canceled | executed | all
        Filter by ticker or event_ticker.
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/portfolio/orders", params=params)

    async def get_order(self, order_id: str) -> Dict:
        """Fetch a single order by order ID."""
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def create_order(self, ticker: str, side: str, action: str,
                           count: int, price: int,
                           order_type: str = "limit",
                           time_in_force: str = "gtc",
                           client_order_id: str = "") -> Dict:
        """
        Place an order.
        side: "yes" | "no"
        action: "buy" | "sell"
        count: number of contracts
        price: price in cents (1–99)
        order_type: "limit" | "market" | "fill_or_kill"
        time_in_force: "gtc" | "ioc" | "fok"
        """
        price_key = "yes_price" if side == "yes" else "no_price"
        body = {
            "ticker":           ticker,
            "side":             side,
            "action":           action,
            "count":            count,
            "type":             order_type,
            price_key:          price,
            "time_in_force":    time_in_force,
            "client_order_id":  client_order_id or f"bot_{int(time.time() * 1000)}",
        }
        return await self._request("POST", "/portfolio/orders", body=body)

    async def cancel_order(self, order_id: str) -> Dict:
        """Cancel a single open order."""
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def cancel_all_orders(self, ticker: str = "", event_ticker: str = "") -> Dict:
        """
        Cancel all resting orders. Optionally filter to a single market or event.
        Returns list of cancelled order IDs.
        """
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return await self._request("DELETE", "/portfolio/orders", params=params)

    async def decrease_order(self, order_id: str, reduce_by: int) -> Dict:
        """
        Decrease the size of a resting order without cancelling it.
        reduce_by: number of contracts to remove from the order.
        """
        return await self._request(
            "POST", f"/portfolio/orders/{order_id}/decrease",
            body={"reduce_by": reduce_by},
        )

    async def batch_create_orders(self, orders: List[Dict]) -> Dict:
        """
        Place multiple orders in a single API call (more efficient for live trading).
        Each dict: {ticker, side, action, count, yes_price/no_price, type, time_in_force}
        """
        return await self._request("POST", "/portfolio/orders/batched", body={"orders": orders})

    async def batch_cancel_orders(self, order_ids: List[str]) -> Dict:
        """Cancel multiple orders by ID in one call."""
        return await self._request(
            "DELETE", "/portfolio/orders/batched",
            body={"order_ids": order_ids},
        )

    # ── Portfolio — Fills & Settlements ──────────────────────────────────────

    async def get_fills(self, ticker: str = "", event_ticker: str = "",
                         limit: int = 100, cursor: str = "",
                         min_ts: int = 0, max_ts: int = 0) -> Dict:
        """
        Trade fills (executed orders). Shows actual fill price and quantity.
        min_ts / max_ts: unix timestamps to filter by time range.
        """
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts:
            params["min_ts"] = min_ts
        if max_ts:
            params["max_ts"] = max_ts
        return await self._request("GET", "/portfolio/fills", params=params)

    async def get_settlements(self, limit: int = 100, cursor: str = "") -> Dict:
        """
        Settled positions — shows P&L for each resolved market.
        Use this for win/loss accounting.
        """
        params: Dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/portfolio/settlements", params=params)

    # ── Exchange info (public) ─────────────────────────────────────────────────

    async def get_exchange_status(self) -> Dict:
        """Trading halt / maintenance status. Check before placing orders live."""
        return await self._request("GET", "/exchange/status")

    async def get_exchange_schedule(self) -> Dict:
        """Exchange trading hours and holiday schedule."""
        return await self._request("GET", "/exchange/schedule")

    # ── Aliases for backward-compat with cli.py ──────────────────────────────

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        return await self.get_market_orderbook(ticker, depth)

    async def place_order(self, ticker: str, side: str, action: str,
                          count: int, type_: str = "limit",
                          yes_price: int = 0, no_price: int = 0,
                          client_order_id: str = "") -> Dict:
        price = yes_price if side == "yes" else no_price
        kwargs = dict(ticker=ticker, side=side, action=action,
                      count=count, price=price, order_type=type_)
        if client_order_id:
            kwargs["client_order_id"] = client_order_id
        return await self.create_order(**kwargs)

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
