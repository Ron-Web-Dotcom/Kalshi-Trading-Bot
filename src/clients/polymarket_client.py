"""
Polymarket client — market data via public Gamma API, orders via CLOB API.

Market data requires NO authentication (public endpoints).
Order placement requires API key + secret (only used when POLY_LIVE_TRADING=true).
"""

import base64
import json
import logging
import os
import random
import time
from typing import Dict, List, Optional

import httpx
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.polymarket_client")


def _normalize_poly_ts(ts: str) -> str:
    """
    Polymarket API sometimes returns ET timestamps with a hardcoded -05:00 (EST) offset
    even during summer (EDT = -04:00). Re-interpret the naive datetime as America/New_York
    so DST is applied correctly, giving the true UTC equivalent.
    Only touches non-UTC timestamps (those with a non-zero UTC offset).
    """
    if not ts:
        return ts
    try:
        from datetime import datetime, timezone
        import zoneinfo
        cd = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if cd.tzinfo is None:
            return ts
        if cd.utcoffset().total_seconds() == 0:
            return ts
        et = zoneinfo.ZoneInfo("America/New_York")
        naive = cd.replace(tzinfo=None)
        cd_et = naive.replace(tzinfo=et)
        return cd_et.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ts


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
_TIMEOUT   = httpx.Timeout(20.0)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

_PROXY_BASE  = os.environ.get("POLY_PROXY_URL", "")
_PROXY_PORTS = list(range(10001, 10008))


class PolymarketTradingClient:
    """
    Full Polymarket client.
    - Gamma API  : public market data, no auth
    - CLOB API   : balance, orders, trades, order book, pricing — auth required for writes
    """

    def __init__(self):
        from src.config.settings import settings
        cfg               = settings.polymarket
        self.key_id       = cfg.api_key
        self.secret_b64   = cfg.api_secret
        self.passphrase   = cfg.api_passphrase
        self.live         = cfg.live_trading_enabled
        self._http: Optional[httpx.AsyncClient] = None
        self._signing_key = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            proxy = None
            if _PROXY_BASE:
                port = random.choice(_PROXY_PORTS)
                import re
                proxy = re.sub(r':\d+(/?)$', f':{port}\\1', _PROXY_BASE)
                logger.debug("Polymarket: proxy exit port %d", port)
            ua = random.choice(_USER_AGENTS)
            self._http = httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": ua, "Accept": "application/json"},
                proxy=proxy,
                trust_env=False,
            )
        return self._http

    def _fresh_client(self) -> httpx.AsyncClient:
        """One-shot client for calls that don't need persistent sessions."""
        return httpx.AsyncClient(
            timeout=_TIMEOUT,
            headers={"User-Agent": random.choice(_USER_AGENTS), "Accept": "application/json"},
            trust_env=False,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # GAMMA API — public market data
    # ─────────────────────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 500) -> List[Dict]:
        """Fetch active Polymarket markets closing TODAY (ET) from Gamma API."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        _now_et  = datetime.now(ZoneInfo("America/New_York"))
        _eod_et  = _now_et.replace(hour=23, minute=59, second=59, microsecond=0)
        _now_utc = _now_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc = _eod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info("Polymarket: fetching TODAY's markets (Gamma, limit=%d, until %s UTC)", limit, _eod_utc)
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{GAMMA_BASE}/markets", params={
                    "active":       "true",
                    "closed":       "false",
                    "limit":        limit,
                    "end_date_min": _now_utc,
                    "end_date_max": _eod_utc,
                })
            if r.status_code != 200:
                logger.warning("Polymarket Gamma HTTP %d — %s", r.status_code, r.text[:200])
                return []

            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
            markets = [p for m in items if (p := self._parse_market(m))]

            logger.info("Polymarket: %d tradeable from %d raw (today ET)", len(markets), len(items))
            return markets
        except Exception as e:
            logger.warning("Polymarket get_markets failed: %s", e)
            return []

    async def get_live_now_markets(self, max_markets: int = 500) -> List[Dict]:
        """Fetch markets closing within the next 2 hours (live events only)."""
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo
        _now_et  = datetime.now(ZoneInfo("America/New_York"))
        _cut_et  = _now_et + timedelta(hours=2)
        _now_utc = _now_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc = _cut_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{GAMMA_BASE}/markets", params={
                    "active":       "true",
                    "closed":       "false",
                    "end_date_min": _now_utc,
                    "end_date_max": _eod_utc,
                    "limit":        max_markets,
                })
            if r.status_code != 200:
                logger.warning("Polymarket live-now HTTP %d", r.status_code)
                return []

            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
            live  = []
            for m in items:
                parsed = self._parse_market(m)
                if parsed and (parsed.get("yes_ask") or 0) > 1:
                    parsed["_poly_live"] = True
                    live.append(parsed)

            logger.info("Polymarket LIVE NOW: %d markets closing ≤2h (from %d raw)", len(live), len(items))
            return live
        except Exception as e:
            logger.warning("Polymarket get_live_now_markets failed: %s", e)
            return []

    async def get_live_markets(self, max_hours: float = 6.0, max_markets: int = 500) -> List[Dict]:
        """Delegate to get_live_now_markets (today-only, ≤2h window)."""
        try:
            markets = await self.get_live_now_markets(max_markets=max_markets)
            logger.info("Polymarket get_live_markets: %d markets", len(markets))
            return markets
        except Exception as e:
            logger.warning("Failed to fetch Polymarket live markets: %s", e)
            return []

    async def search_markets(self, query: str, limit: int = 20) -> List[Dict]:
        """Search markets, events, and profiles via Gamma API."""
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{GAMMA_BASE}/markets", params={"search": query, "limit": limit, "active": "true"})
            if r.status_code != 200:
                return []
            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
            return [p for m in items if (p := self._parse_market(m))]
        except Exception as e:
            logger.warning("Polymarket search_markets failed: %s", e)
            return []

    async def get_market_by_slug(self, slug: str) -> Optional[Dict]:
        """Fetch a single market by slug from Gamma API."""
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{GAMMA_BASE}/markets", params={"slug": slug, "limit": 1})
            if r.status_code != 200:
                return None
            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
            return self._parse_market(items[0]) if items else None
        except Exception as e:
            logger.debug("get_market_by_slug %s: %s", slug, e)
            return None

    async def get_market_by_token(self, token_id: str) -> Optional[Dict]:
        """Fetch market by token/condition ID — checks active + closed for resolution pricing."""
        for params in [
            {"clob_token_ids": token_id, "limit": 1},
            {"clob_token_ids": token_id, "limit": 1, "closed": "true"},
            {"clob_token_ids": token_id, "limit": 1, "active": "false"},
        ]:
            try:
                async with self._fresh_client() as _c:
                    r = await _c.get(f"{GAMMA_BASE}/markets", params=params)
                if r.status_code != 200:
                    continue
                raw   = r.json()
                items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
                if items:
                    return self._parse_market(items[0])
            except Exception as e:
                logger.debug("get_market_by_token %s: %s", token_id[:16], e)

        # Fallback: CLOB last-trade-price
        price = await self.get_last_trade_price(token_id)
        if price is not None:
            return {"yes_ask": price * 100, "no_ask": (1 - price) * 100, "last_price": price * 100}
        return None

    async def get_events(self, limit: int = 200) -> List[Dict]:
        """Fetch open events from Gamma API (each event contains multiple markets)."""
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{GAMMA_BASE}/events", params={"active": "true", "limit": limit})
            if r.status_code != 200:
                return []
            raw    = r.json()
            events = raw if isinstance(raw, list) else (raw.get("data") or raw.get("events") or [])
            result = []
            for ev in events:
                for m in (ev.get("markets") or []):
                    if isinstance(m, dict):
                        m.setdefault("category", ev.get("category", ""))
                        m.setdefault("question", m.get("question") or ev.get("title", ""))
                        parsed = self._parse_market(m)
                        if parsed:
                            result.append(parsed)
            return result
        except Exception as e:
            logger.warning("Polymarket get_events failed: %s", e)
            return []

    def _parse_market(self, m: Dict) -> Optional[Dict]:
        """Parse one Gamma API market object into our standard format."""
        import json as _json
        try:
            raw_prices = m.get("outcomePrices") or []
            if isinstance(raw_prices, str):
                try:
                    raw_prices = _json.loads(raw_prices)
                except Exception:
                    raw_prices = []

            yes_price, no_price = 0.0, 0.0
            if len(raw_prices) >= 2:
                try:
                    p0 = float(raw_prices[0])
                    p1 = float(raw_prices[1])
                    yes_price = p0 * 100 if p0 <= 1.0 else p0
                    no_price  = p1 * 100 if p1 <= 1.0 else p1
                except (TypeError, ValueError):
                    pass
            elif len(raw_prices) == 1:
                try:
                    p0 = float(raw_prices[0])
                    yes_price = p0 * 100 if p0 <= 1.0 else p0
                    no_price  = 100 - yes_price
                except (TypeError, ValueError):
                    pass

            if yes_price == 0:
                bid = m.get("bestBid") or 0
                ask = m.get("bestAsk") or 0
                val = float(ask or bid or 0)
                yes_price = val * 100 if val <= 1.0 else val
                no_price  = 100 - yes_price

            if yes_price == 0:
                lp  = m.get("lastTradePrice") or m.get("lastPrice") or 0
                val = float(lp or 0)
                yes_price = val * 100 if val <= 1.0 else val
                no_price  = 100 - yes_price

            if yes_price == 0 and no_price == 0:
                return None

            raw_vol = m.get("volume") or m.get("volumeNum") or 0
            try:
                volume = float(raw_vol)
            except (TypeError, ValueError):
                volume = 0.0

            token_ids = m.get("clobTokenIds") or m.get("tokenIds") or []

            ticker = str(
                m.get("conditionId")
                or m.get("id")
                or m.get("slug")
                or m.get("marketMakerAddress")
                or ""
            ).strip()
            if not ticker:
                question = m.get("question") or m.get("description") or m.get("title") or ""
                if question:
                    import hashlib
                    ticker = "poly_" + hashlib.md5(question.encode()).hexdigest()[:12]
                else:
                    return None

            return {
                "platform":      "polymarket",
                "ticker":        ticker,
                "slug":          m.get("slug", ""),
                "title":         m.get("question") or m.get("description") or m.get("title", ""),
                "category":      (m.get("category") or m.get("groupItemTitle") or "").lower(),
                "yes_ask":       round(yes_price, 1),
                "no_ask":        round(no_price,  1),
                "yes_bid":       round(max(yes_price - 1, 1), 1),
                "no_bid":        round(max(no_price  - 1, 1), 1),
                "volume":        volume,
                "close_time":    _normalize_poly_ts(m.get("endDate") or m.get("endDateIso", "")),
                "open_interest": float(m.get("openInterest") or 0),
                "status":        "open",
                "_yes_token":    token_ids[0] if len(token_ids) > 0 else None,
                "_no_token":     token_ids[1] if len(token_ids) > 1 else None,
            }
        except Exception as e:
            logger.debug("Polymarket parse error: %s — %s", e, str(m)[:100])
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # CLOB API — Orderbook & Pricing (public, no auth)
    # ─────────────────────────────────────────────────────────────────────────

    async def get_clob_markets(self, limit: int = 500) -> List[Dict]:
        """
        CLOB simplified markets — authoritative source for tokenIDs.
        Use this when placing orders to get the correct tokenID.
        """
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/markets", params={"limit": limit})
            if r.status_code != 200:
                logger.warning("CLOB markets HTTP %d", r.status_code)
                return []
            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or [])
            result = []
            for m in items:
                token_id = m.get("condition_id") or m.get("tokenId") or ""
                if token_id:
                    result.append({
                        "token_id":   token_id,
                        "question":   m.get("question", ""),
                        "active":     m.get("active", True),
                        "closed":     m.get("closed", False),
                        "tokens":     m.get("tokens", []),
                    })
            logger.info("CLOB markets: %d returned", len(result))
            return result
        except Exception as e:
            logger.warning("get_clob_markets failed: %s", e)
            return []

    async def get_order_book(self, token_id: str) -> Optional[Dict]:
        """
        Full order book for a token — bids and asks with sizes.
        Returns {"bids": [...], "asks": [...], "market": token_id}
        """
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/book", params={"token_id": token_id})
            if r.status_code != 200:
                logger.debug("get_order_book %s HTTP %d", token_id[:16], r.status_code)
                return None
            data = r.json()
            return {
                "market": token_id,
                "bids":   data.get("bids", []),    # [{"price": "0.55", "size": "100"}, ...]
                "asks":   data.get("asks", []),
            }
        except Exception as e:
            logger.debug("get_order_book %s: %s", token_id[:16], e)
            return None

    async def get_market_price(self, token_id: str) -> Optional[float]:
        """
        Best ask price for a token from CLOB (0.0–1.0 scale).
        Multiply by 100 to get cents.
        """
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "buy"})
            if r.status_code != 200:
                return None
            data  = r.json()
            price = data.get("price")
            return float(price) if price is not None else None
        except Exception as e:
            logger.debug("get_market_price %s: %s", token_id[:16], e)
            return None

    async def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """
        True mid price between best bid and best ask (0.0–1.0).
        Better than yes_ask for edge calculations — use this for fair value.
        """
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
            if r.status_code != 200:
                return None
            data = r.json()
            mid  = data.get("mid")
            return float(mid) if mid is not None else None
        except Exception as e:
            logger.debug("get_midpoint_price %s: %s", token_id[:16], e)
            return None

    async def get_spread(self, token_id: str) -> Optional[float]:
        """Bid-ask spread for a token (0.0–1.0). High spread = illiquid, avoid."""
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/spread", params={"token_id": token_id})
            if r.status_code != 200:
                return None
            data   = r.json()
            spread = data.get("spread")
            return float(spread) if spread is not None else None
        except Exception as e:
            logger.debug("get_spread %s: %s", token_id[:16], e)
            return None

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Last executed trade price for a token (0.0–1.0)."""
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/last-trade-price", params={"token_id": token_id})
            if r.status_code != 200:
                return None
            data  = r.json()
            price = data.get("price")
            return float(price) if price is not None else None
        except Exception as e:
            logger.debug("get_last_trade_price %s: %s", token_id[:16], e)
            return None

    async def get_prices_history(self, token_id: str, interval: str = "1h", fidelity: int = 60) -> List[Dict]:
        """
        Price history for a token.
        interval: "1m", "5m", "1h", "6h", "1d"
        fidelity: data points (60 = 60 points over the interval)
        Returns list of {"t": timestamp, "p": price}
        """
        try:
            async with self._fresh_client() as _c:
                r = await _c.get(f"{CLOB_BASE}/prices-history", params={
                    "market":   token_id,
                    "interval": interval,
                    "fidelity": fidelity,
                })
            if r.status_code != 200:
                return []
            data    = r.json()
            history = data.get("history") or data if isinstance(data, list) else []
            return history
        except Exception as e:
            logger.debug("get_prices_history %s: %s", token_id[:16], e)
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # CLOB API — Account (auth required)
    # ─────────────────────────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """USDC balance from CLOB API."""
        if not self.key_id:
            logger.warning("Polymarket balance: POLY_API_KEY not set")
            return None
        if not self.secret_b64:
            logger.warning("Polymarket balance: POLY_API_SECRET not set")
            return None
        try:
            # Primary: GET /balance (returns {"balance": "171.50"})
            path = "/balance"
            r    = await self._client().get(f"{CLOB_BASE}{path}", headers=self._auth_headers("GET", path))
            if r.status_code == 200:
                data = r.json()
                bal  = float(data.get("balance", 0))
                logger.info("Polymarket USDC balance: $%.2f", bal)
                return bal
            if r.status_code == 401:
                logger.warning("Polymarket balance 401 — check POLY_API_KEY / POLY_API_SECRET / POLY_API_PASSPHRASE")
                logger.debug("balance 401 body: %s", r.text[:200])
                return None
            if r.status_code == 404:
                # Fallback: wallet address path
                from src.config.settings import settings as _s
                wallet = _s.polymarket.wallet_address
                if wallet:
                    r2 = await self._client().get(
                        f"{CLOB_BASE}/balance",
                        params={"address": wallet},
                        headers=self._auth_headers("GET", "/balance"),
                    )
                    if r2.status_code == 200:
                        data = r2.json()
                        bal  = float(data.get("balance", 0))
                        logger.info("Polymarket USDC balance (address param): $%.2f", bal)
                        return bal
                logger.warning("Polymarket balance: 404 — endpoint not found, check CLOB_BASE")
                return None
            logger.warning("Polymarket balance HTTP %d: %s", r.status_code, r.text[:100])
            return None
        except Exception as e:
            logger.warning("Polymarket balance check failed: %s", e)
            return None

    async def get_open_orders(self, market: Optional[str] = None) -> List[Dict]:
        """
        All open orders. Optionally filter by market (condition_id / token_id).
        Returns list of order dicts.
        """
        if not self.key_id or not self.secret_b64:
            return []
        try:
            path   = "/orders"
            params = {}
            if market:
                params["market"] = market
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                params=params,
                headers=self._auth_headers("GET", path),
            )
            if r.status_code != 200:
                logger.warning("get_open_orders HTTP %d: %s", r.status_code, r.text[:100])
                return []
            data = r.json()
            return data if isinstance(data, list) else (data.get("data") or [])
        except Exception as e:
            logger.warning("get_open_orders failed: %s", e)
            return []

    async def get_order(self, order_id: str) -> Optional[Dict]:
        """Fetch a single order by ID."""
        if not self.key_id or not self.secret_b64:
            return None
        try:
            path = f"/orders/{order_id}"
            r    = await self._client().get(f"{CLOB_BASE}{path}", headers=self._auth_headers("GET", path))
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            logger.debug("get_order %s: %s", order_id, e)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single open order by ID. Returns True on success."""
        if not self.live:
            logger.info("[POLY PAPER] cancel_order %s (simulated)", order_id)
            return True
        if not self.key_id or not self.secret_b64:
            return False
        try:
            path = f"/orders/{order_id}"
            r    = await self._client().delete(f"{CLOB_BASE}{path}", headers=self._auth_headers("DELETE", path))
            if r.status_code in (200, 204):
                logger.info("Polymarket: cancelled order %s", order_id)
                return True
            logger.warning("cancel_order %s HTTP %d: %s", order_id, r.status_code, r.text[:100])
            return False
        except Exception as e:
            logger.warning("cancel_order %s failed: %s", order_id, e)
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders. Returns True on success."""
        if not self.live:
            logger.info("[POLY PAPER] cancel_all_orders (simulated)")
            return True
        if not self.key_id or not self.secret_b64:
            return False
        try:
            path = "/orders"
            r    = await self._client().delete(f"{CLOB_BASE}{path}", headers=self._auth_headers("DELETE", path))
            if r.status_code in (200, 204):
                logger.info("Polymarket: all orders cancelled")
                return True
            logger.warning("cancel_all_orders HTTP %d: %s", r.status_code, r.text[:100])
            return False
        except Exception as e:
            logger.warning("cancel_all_orders failed: %s", e)
            return False

    async def cancel_orders_for_market(self, market: str) -> bool:
        """Cancel all orders for a specific market (condition_id)."""
        if not self.live:
            logger.info("[POLY PAPER] cancel_orders_for_market %s (simulated)", market[:16])
            return True
        if not self.key_id or not self.secret_b64:
            return False
        try:
            path = f"/orders/market/{market}"
            r    = await self._client().delete(f"{CLOB_BASE}{path}", headers=self._auth_headers("DELETE", path))
            if r.status_code in (200, 204):
                logger.info("Polymarket: cancelled orders for market %s", market[:16])
                return True
            logger.warning("cancel_orders_for_market HTTP %d: %s", r.status_code, r.text[:100])
            return False
        except Exception as e:
            logger.warning("cancel_orders_for_market failed: %s", e)
            return False

    async def get_trades(self, limit: int = 50, market: Optional[str] = None) -> List[Dict]:
        """
        Fetch trade history from CLOB.
        market: optional condition_id to filter to one market.
        Returns list of trade dicts with timestamp, side, price, size.
        """
        if not self.key_id or not self.secret_b64:
            return []
        try:
            path   = "/trades"
            params: Dict = {"limit": limit}
            if market:
                params["market"] = market
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                params=params,
                headers=self._auth_headers("GET", path),
            )
            if r.status_code != 200:
                logger.warning("get_trades HTTP %d: %s", r.status_code, r.text[:100])
                return []
            data = r.json()
            return data if isinstance(data, list) else (data.get("data") or [])
        except Exception as e:
            logger.warning("get_trades failed: %s", e)
            return []

    async def get_order_scoring_status(self, order_id: str) -> Optional[Dict]:
        """Check the scoring/fill status of an order."""
        if not self.key_id or not self.secret_b64:
            return None
        try:
            path = f"/orders/{order_id}/scoring"
            r    = await self._client().get(f"{CLOB_BASE}{path}", headers=self._auth_headers("GET", path))
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            logger.debug("get_order_scoring_status %s: %s", order_id, e)
            return None

    async def send_heartbeat(self) -> bool:
        """
        Send CLOB heartbeat — keeps authenticated session alive.
        Call this every ~30s when actively trading live.
        """
        if not self.key_id or not self.secret_b64:
            return False
        try:
            path = "/heartbeat"
            r    = await self._client().post(f"{CLOB_BASE}{path}", headers=self._auth_headers("POST", path))
            return r.status_code in (200, 204)
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # CLOB API — Order placement (live only)
    # ─────────────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        token_id:    str,
        side:        str,
        price_cents: float,
        size_usdc:   float,
    ) -> Optional[Dict]:
        """
        Paper mode : log and return simulated fill.
        Live mode  : submit real GTC order to CLOB.
        side: "BUY" (YES) or "SELL" (NO)
        price_cents: 1–99 (will be converted to 0.01–0.99 fraction)
        size_usdc: dollar amount to spend
        """
        if not self.live:
            logger.info(
                "[POLY PAPER] %s token=%s @ %.0f¢ $%.2f (simulated)",
                side.upper(), (token_id or "?")[:16], price_cents, size_usdc,
            )
            return {"simulated": True, "token_id": token_id, "price": price_cents}

        if not self.key_id or not self.secret_b64:
            logger.error("POLY LIVE requires POLY_API_KEY + POLY_API_SECRET in .env")
            return None

        price_frac = price_cents / 100.0
        if price_frac <= 0 or price_frac >= 1:
            logger.error("POLY LIVE: invalid price_cents=%.1f — must be 1–99", price_cents)
            return None

        from src.config.settings import settings as _s
        wallet = _s.polymarket.wallet_address
        if not wallet:
            logger.error("POLY LIVE: POLY_WALLET_ADDRESS not set in .env")
            return None

        shares     = round(size_usdc / price_frac, 2)
        expiration = int(time.time()) + 300
        nonce      = int(time.time() * 1000)

        body_dict = {
            "order": {
                "tokenID":    token_id,
                "price":      str(round(price_frac, 4)),
                "size":       str(shares),
                "side":       side.upper(),
                "type":       "GTC",
                "feeRateBps": "0",
                "nonce":      str(nonce),
                "expiration": str(expiration),
                "maker":      wallet,
            }
        }
        body = json.dumps(body_dict)
        path = "/order"
        try:
            r = await self._client().post(
                f"{CLOB_BASE}{path}",
                content=body,
                headers=self._auth_headers("POST", path, body),
            )
            if r.status_code != 200:
                logger.error("POLY LIVE order HTTP %d — %s", r.status_code, r.text[:300])
                return None
            resp = r.json()
            logger.info(
                "POLY LIVE ORDER: %s @ %.0f¢ $%.2f → orderID=%s",
                side.upper(), price_cents, size_usdc, resp.get("orderID", "?"),
            )
            return resp
        except Exception as e:
            logger.error("Polymarket live order failed: %s", e)
            return None

    async def place_multiple_orders(self, orders: List[Dict]) -> Optional[List[Dict]]:
        """
        Batch order placement — more efficient than calling place_order N times.
        Each order dict: {token_id, side, price_cents, size_usdc}
        """
        if not self.live:
            results = []
            for o in orders:
                results.append(await self.place_order(
                    o["token_id"], o["side"], o["price_cents"], o["size_usdc"]
                ))
            return results

        if not self.key_id or not self.secret_b64:
            return None

        from src.config.settings import settings as _s
        wallet = _s.polymarket.wallet_address
        if not wallet:
            logger.error("POLY LIVE: POLY_WALLET_ADDRESS not set")
            return None

        order_list = []
        for o in orders:
            price_frac = o["price_cents"] / 100.0
            shares     = round(o["size_usdc"] / price_frac, 2)
            order_list.append({
                "tokenID":    o["token_id"],
                "price":      str(round(price_frac, 4)),
                "size":       str(shares),
                "side":       o["side"].upper(),
                "type":       "GTC",
                "feeRateBps": "0",
                "nonce":      str(int(time.time() * 1000)),
                "expiration": str(int(time.time()) + 300),
                "maker":      wallet,
            })

        body = json.dumps({"orders": order_list})
        path = "/orders"
        try:
            r = await self._client().post(
                f"{CLOB_BASE}{path}",
                content=body,
                headers=self._auth_headers("POST", path, body),
            )
            if r.status_code != 200:
                logger.error("POLY LIVE batch orders HTTP %d — %s", r.status_code, r.text[:300])
                return None
            resp = r.json()
            logger.info("POLY LIVE BATCH: %d orders placed", len(order_list))
            return resp if isinstance(resp, list) else [resp]
        except Exception as e:
            logger.error("Polymarket batch orders failed: %s", e)
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Convenience helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def get_best_price(self, token_id: str, side: str = "buy") -> Optional[float]:
        """
        Get the best available price for a token from the order book.
        side: "buy" (best ask) or "sell" (best bid)
        Returns price in cents (0–100).
        """
        book = await self.get_order_book(token_id)
        if not book:
            # Fallback to price endpoint
            p = await self.get_market_price(token_id)
            return p * 100 if p is not None else None

        if side == "buy":
            asks = book.get("asks", [])
            if asks:
                return float(asks[0].get("price", 0)) * 100
        else:
            bids = book.get("bids", [])
            if bids:
                return float(bids[0].get("price", 0)) * 100
        return None

    async def get_fair_value(self, token_id: str) -> Optional[float]:
        """
        Fair value in cents — uses CLOB midpoint (best signal for edge calc).
        Falls back to last trade price if midpoint unavailable.
        """
        mid = await self.get_midpoint_price(token_id)
        if mid is not None:
            return mid * 100
        ltp = await self.get_last_trade_price(token_id)
        if ltp is not None:
            return ltp * 100
        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Auth helpers — HMAC-SHA256 L2 authentication
    #
    # Polymarket CLOB uses L2 auth:
    #   POLY-ACCESS-TOKEN  : api_key        (from POLY_API_KEY)
    #   POLY-TIMESTAMP     : unix seconds   (NOT milliseconds)
    #   POLY-SIGNATURE     : base64( hmac_sha256(secret, ts+METHOD+path+body) )
    #   POLY-PASSPHRASE    : passphrase     (from POLY_API_PASSPHRASE)
    #
    # Keys are generated via Polymarket's API key management page or py-clob-client.
    # POLY_API_SECRET is the raw base64-encoded secret (32 bytes).
    # ─────────────────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """HMAC-SHA256 signature over (timestamp + METHOD + path + body)."""
        import hmac as _hmac
        import hashlib as _hl
        if not self.secret_b64:
            return ""
        try:
            secret = base64.b64decode(self.secret_b64)
        except Exception:
            secret = self.secret_b64.encode()
        msg = (timestamp + method.upper() + path + body).encode()
        return base64.b64encode(_hmac.new(secret, msg, _hl.sha256).digest()).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts  = str(int(time.time()))   # seconds, not milliseconds
        sig = self._sign(ts, method, path, body)
        headers = {
            "POLY-ACCESS-TOKEN": self.key_id,
            "POLY-TIMESTAMP":    ts,
            "POLY-SIGNATURE":    sig,
            "Content-Type":      "application/json",
        }
        if self.passphrase:
            headers["POLY-PASSPHRASE"] = self.passphrase
        return headers

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
