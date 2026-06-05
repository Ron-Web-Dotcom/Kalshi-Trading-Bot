"""Kalshi API v2 client — RSA-signed auth with simple API key fallback."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("trading.kalshi_client")


def _load_private_key(pem_text: str):
    """Load RSA private key from PEM string for request signing."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
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
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + path + body
        sig = self._private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
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
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.monotonic()

    async def _request(self, method: str, path: str, params: Optional[Dict] = None,
                       body: Optional[Dict] = None, retries: int = 3) -> Any:
        await self._rate_limit()
        client = await self._get_client()
        body_str = json.dumps(body) if body else ""
        # Kalshi RSA signature must include query string when present
        sign_path = path
        if params:
            sign_path = path + "?" + urlencode({k: v for k, v in params.items() if v is not None})
        headers = self._build_headers(method, sign_path, body_str)

        for attempt in range(retries):
            try:
                resp = await client.request(
                    method, path, params=params,
                    content=body_str.encode() if body_str else None,
                    headers=headers
                )
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    # Auth failure — log once and return empty so cycle continues
                    logger.warning("Kalshi 401 on %s %s — check API key/RSA key config", method, path)
                    return {}
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

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 200, cursor: str = "", status: str = "open") -> Dict:
        params: Dict[str, Any] = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
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

        # Strategy 1: /events endpoint — Kalshi groups markets under events
        # An event being "active" means the underlying game/match is live NOW
        try:
            data = await self._request("GET", "/events", params={
                "status": "open",
                "limit": 200,
            })
            events = data.get("events") or []
            for event in events:
                # Events with a live score or "in_game" flag are truly live
                event_status = (event.get("event_status") or "").lower()
                mutually_ex  = event.get("mutually_exclusive_restriction")
                # Kalshi sets category to "Sports" and has live game data for live events
                category = (event.get("category") or "").lower()
                title    = event.get("title") or event.get("event_title") or ""
                if not title:
                    continue
                # Check for live indicator in the event data
                if event_status in ("live", "active", "in_progress", "in_game"):
                    # Pull the sub-markets for this event
                    for m in (event.get("markets") or []):
                        if isinstance(m, dict):
                            m.setdefault("title", title)
                            m.setdefault("category", category)
                            m["_kalshi_live"] = True
                            live_markets.append(m)
        except Exception as e:
            logger.debug("Kalshi /events live fetch: %s", e)

        if live_markets:
            logger.info("Kalshi LIVE NOW (/events): %d live markets", len(live_markets))
            return live_markets[:max_markets]

        # Strategy 2: /markets with live-specific series tickers
        # Kalshi live sports markets have series tickers like SOCCER-*, NBA-LIVE-*, etc.
        live_series_prefixes = [
            "SOCCER", "NFL", "NBA", "MLB", "NHL", "UFC", "TENNIS",
            "F1", "GOLF", "RUGBY", "CRICKET", "BOXING",
        ]
        try:
            markets = []
            cursor = ""
            while len(markets) < max_markets:
                data = await self.get_markets(limit=200, cursor=cursor, status="open")
                batch = data.get("markets") or []
                if not batch:
                    break
                markets.extend(batch)
                cursor = data.get("cursor") or ""
                if not cursor:
                    break
                await asyncio.sleep(0.1)

            now = datetime.now(timezone.utc)
            for m in markets:
                ticker = (m.get("ticker") or "").upper()
                title  = (m.get("title") or "").lower()
                ct     = m.get("close_time") or ""
                # Must still be open
                try:
                    close_dt = datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if hours_left <= 0:
                        continue
                except Exception:
                    continue
                # Live sports markets on Kalshi close when the game ends (usually <3h)
                # and have sport-related tickers or titles
                is_sport_ticker = any(ticker.startswith(p) for p in live_series_prefixes)
                is_sport_title  = any(kw in title for kw in [
                    "vs ", " vs", "match", "game", "quarter", "half", "period",
                    "inning", "set ", "round", "bout", "race", "leg ",
                ])
                if (is_sport_ticker or is_sport_title) and hours_left <= 6:
                    m["_kalshi_live"] = True
                    m["hours_to_close"] = round(hours_left, 2)
                    live_markets.append(m)

        except Exception as e:
            logger.debug("Kalshi live sports market scan: %s", e)

        logger.info("Kalshi LIVE NOW (sports scan): %d confirmed live markets", len(live_markets))
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
                    return datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
                except Exception:
                    return datetime.max.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
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
        now = datetime.now(timezone.utc)
        cutoff = (now.replace(tzinfo=timezone.utc)
                  if now.tzinfo else now.replace(tzinfo=timezone.utc))

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
                        return f if f <= 1.0 else f / 100.0
                    except Exception:
                        return 0.0
                for r in (rows or []):
                    m = dict(r)
                    yes_ask = _norm(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid"))
                    if not yes_ask:
                        continue  # skip truly priceless markets
                    no_ask = _norm(m.get("no_ask")) or round(1.0 - yes_ask, 3)
                    try:
                        close_dt = datetime.fromisoformat(str(m["close_time"]).replace("Z", "+00:00"))
                        if close_dt.tzinfo is None:
                            close_dt = close_dt.replace(tzinfo=timezone.utc)
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
                        close_dt = close_dt.replace(tzinfo=timezone.utc)
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if not (0 < hours_left <= max_hours):
                        continue
                except Exception:
                    continue
                # Kalshi API prices can be in cents (int) or decimal — normalise both
                def _p(v):
                    try:
                        f = float(v or 0)
                        return f if f <= 1.0 else f / 100.0  # convert cents→fraction if >1
                    except Exception:
                        return 0.0
                yes_ask = _p(m.get("yes_ask") or m.get("last_price") or m.get("yes_bid"))
                no_ask  = _p(m.get("no_ask")) or round(1.0 - yes_ask, 3)
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

    async def get_market(self, ticker: str) -> Dict:
        return await self._request("GET", f"/markets/{ticker}")

    async def get_market_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_balance(self) -> Dict:
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> Dict:
        return await self._request("GET", "/portfolio/positions")

    async def get_orders(self, status: str = "") -> Dict:
        params = {}
        if status:
            params["status"] = status
        return await self._request("GET", "/portfolio/orders", params=params)

    async def create_order(self, ticker: str, side: str, action: str,
                           count: int, price: int,
                           order_type: str = "limit",
                           time_in_force: str = "gtc") -> Dict:
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "yes_price": price if side == "yes" else (100 - price),
            "no_price": price if side == "no" else (100 - price),
            "time_in_force": time_in_force,
            "client_order_id": f"bot_{int(time.time() * 1000)}",
        }
        return await self._request("POST", "/portfolio/orders", body=body)

    async def cancel_order(self, order_id: str) -> Dict:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    # ── Aliases for backward-compat with cli.py ──────────────────────────────

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict:
        return await self.get_market_orderbook(ticker, depth)

    async def place_order(self, ticker: str, side: str, action: str,
                          count: int, type_: str = "limit",
                          yes_price: int = 0, no_price: int = 0,
                          client_order_id: str = "") -> Dict:
        price = yes_price if side == "yes" else no_price
        return await self.create_order(
            ticker=ticker, side=side, action=action,
            count=count, price=price, order_type=type_
        )

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
