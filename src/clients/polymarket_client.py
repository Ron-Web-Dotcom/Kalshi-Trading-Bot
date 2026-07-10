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
        # If already UTC (offset == 0), leave as-is
        if cd.utcoffset().total_seconds() == 0:
            return ts
        # Re-attach the naive local time as America/New_York (DST-aware)
        et = zoneinfo.ZoneInfo("America/New_York")
        naive = cd.replace(tzinfo=None)
        cd_et = naive.replace(tzinfo=et)
        return cd_et.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ts


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
_TIMEOUT   = httpx.Timeout(20.0)

# Rotate User-Agents to look like normal browser traffic
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Proxy base URL — ports 10001-10007 each route through a different exit IP
_PROXY_BASE = os.environ.get("POLY_PROXY_URL", "")
_PROXY_PORTS = list(range(10001, 10008))  # 10001–10007


class PolymarketTradingClient:
    """
    Polymarket client.
    - get_markets(): public Gamma API, no auth needed
    - place_order(): CLOB API, requires key+secret, only in live mode
    """

    def __init__(self):
        from src.config.settings import settings
        cfg              = settings.polymarket
        self.key_id      = cfg.api_key
        self.secret_b64  = cfg.api_secret
        self.live        = cfg.live_trading_enabled
        self._http: Optional[httpx.AsyncClient] = None
        self._signing_key = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            proxy = None
            if _PROXY_BASE:
                # Rotate through ports 10001-10007 — each is a different exit IP
                port = random.choice(_PROXY_PORTS)
                import re
                proxy = re.sub(r':\d+(/?)$', f':{port}\\1', _PROXY_BASE)
                logger.debug("Polymarket: proxy exit port %d", port)
            ua = random.choice(_USER_AGENTS)
            self._http = httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": ua, "Accept": "application/json"},
                proxy=proxy,
                # Bypass any system-level proxy (causes 407 on some VPS configs)
                trust_env=False,
            )
        return self._http

    # ── Market data (PUBLIC — no auth) ────────────────────────────────────────

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 100) -> List[Dict]:
        """
        Fetch active Polymarket markets closing TODAY (ET) from the public Gamma API.
        Returns list of normalised market dicts ready for the opportunity hunter.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _now_et = datetime.now(ZoneInfo("America/New_York"))
        # end_date_max = tonight midnight UTC (end of today ET)
        _eod_et = _now_et.replace(hour=23, minute=59, second=59, microsecond=0)
        from datetime import timezone
        _eod_utc = _eod_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _now_utc = _now_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info("Polymarket: fetching TODAY's markets from Gamma API (limit=%d, until %s UTC)...", limit, _eod_utc)
        try:
            # Use a fresh client per fetch — avoids stale proxy config / 407 errors
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": random.choice(_USER_AGENTS), "Accept": "application/json"},
                trust_env=False,
            ) as _c:
                r = await _c.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active":        "true",
                        "closed":        "false",
                        "limit":         limit,
                        "end_date_min":  _now_utc,    # must not have already closed
                        "end_date_max":  _eod_utc,    # must close before midnight ET tonight
                    },
                )
            if r.status_code != 200:
                logger.warning(
                    "Polymarket Gamma API HTTP %d — %s",
                    r.status_code, r.text[:200],
                )
                return []

            raw = r.json()
            if isinstance(raw, dict):
                items = raw.get("data") or raw.get("markets") or []
            elif isinstance(raw, list):
                items = raw
            else:
                logger.warning("Polymarket: unexpected response type %s", type(raw))
                return []

            markets = []
            for m in items:
                parsed = self._parse_market(m)
                if parsed:
                    markets.append(parsed)

            if items and not markets:
                sample = items[0]
                logger.warning(
                    "Polymarket: 0 tradeable from %d raw — sample keys: %s",
                    len(items),
                    list(sample.keys())[:15],
                )
            logger.info(
                "Polymarket: %d tradeable markets from %d raw",
                len(markets), len(items),
            )
            return markets

        except Exception as e:
            logger.warning("Polymarket fetch failed: %s", e)
            return []

    def _parse_market(self, m: Dict) -> Optional[Dict]:
        """Parse one Gamma API market object into our standard format."""
        import json as _json
        try:
            # outcomePrices may be a real list OR a JSON-encoded string
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

            # Fallback to bestBid/bestAsk
            if yes_price == 0:
                bid = m.get("bestBid") or 0
                ask = m.get("bestAsk") or 0
                val = float(ask or bid or 0)
                yes_price = val * 100 if val <= 1.0 else val
                no_price  = 100 - yes_price

            # Fallback to last traded price
            if yes_price == 0:
                lp = m.get("lastTradePrice") or m.get("lastPrice") or 0
                val = float(lp or 0)
                yes_price = val * 100 if val <= 1.0 else val
                no_price  = 100 - yes_price

            # Skip markets with no real price data — don't fabricate 50/50
            if yes_price == 0 and no_price == 0:
                return None

            # Volume — Gamma returns in USDC as string or float
            raw_vol = m.get("volume") or m.get("volumeNum") or 0
            try:
                volume = float(raw_vol)
            except (TypeError, ValueError):
                volume = 0.0

            # Token IDs for live order placement
            token_ids = m.get("clobTokenIds") or m.get("tokenIds") or []

            # Accept any available identifier as ticker
            # Gamma API (limited endpoint) may not have conditionId/id — use description hash or outcomes
            ticker = str(
                m.get("conditionId")
                or m.get("id")
                or m.get("slug")
                or m.get("marketMakerAddress")
                or ""
            ).strip()
            if not ticker:
                # Last resort: hash the question text
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
                "open_interest": 0,
                "status":        "open",
                "_yes_token":    token_ids[0] if len(token_ids) > 0 else None,
                "_no_token":     token_ids[1] if len(token_ids) > 1 else None,
            }
        except Exception as e:
            logger.debug("Polymarket parse error: %s — %s", e, str(m)[:100])
            return None

    async def get_live_now_markets(self, max_markets: int = 500) -> List[Dict]:
        """
        Fetch ALL active Polymarket markets that are live RIGHT NOW —
        closing within the next 2 hours. Every category included.
        These are events actually happening at this moment.
        """
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo
        _now_et  = datetime.now(ZoneInfo("America/New_York"))
        _cut_et  = _now_et + timedelta(hours=2)          # 2h window — live events only
        _now_utc = _now_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _eod_utc = _cut_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={"User-Agent": random.choice(_USER_AGENTS), "Accept": "application/json"},
                trust_env=False,
            ) as _c:
                r = await _c.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active":       "true",
                        "closed":       "false",
                        "end_date_min": _now_utc,   # not already closed
                        "end_date_max": _eod_utc,   # closes before midnight ET tonight
                        "limit":        max_markets,
                    },
                )
            if r.status_code != 200:
                logger.warning("Polymarket live-now HTTP %d", r.status_code)
                return []

            raw   = r.json()
            items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
            live_markets = []
            for m in items:
                parsed = self._parse_market(m)
                if not parsed or (parsed.get("yes_ask") or 0) <= 1:
                    continue
                parsed["_poly_live"] = True
                live_markets.append(parsed)

            logger.info(
                "Polymarket LIVE NOW: %d markets closing within 2h (from %d raw)",
                len(live_markets), len(items),
            )
            return live_markets

        except Exception as e:
            logger.warning("Polymarket get_live_now_markets failed: %s", e)
            return []

    async def get_live_markets(self, max_hours: float = 6.0, max_markets: int = 500) -> List[Dict]:
        """
        Return today's markets — get_live_now_markets already filters to today ET.
        max_hours kept for API compatibility but today-only is the effective filter.
        """
        try:
            markets = await self.get_live_now_markets(max_markets=max_markets)
            logger.info("Polymarket get_live_markets: %d today markets", len(markets))
            return markets
        except Exception as e:
            logger.warning("Failed to fetch Polymarket live markets: %s", e)
            return []

    # ── Balance check ──────────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Fetch USDC balance from CLOB API (requires valid credentials)."""
        if not self.key_id:
            logger.warning("Polymarket balance: POLY_API_KEY not set")
            return None
        if not self.secret_b64:
            logger.warning("Polymarket balance: POLY_API_SECRET not set — cannot authenticate")
            return None
        try:
            path = "/balance"
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                headers=self._auth_headers("GET", path),
            )
            if r.status_code == 401:
                logger.warning("Polymarket balance: 401 Unauthorized — check POLY_API_KEY + POLY_API_SECRET")
                return None
            r.raise_for_status()
            data = r.json()
            bal = float(data.get("balance", 0))
            logger.info("Polymarket USDC balance: $%.2f", bal)
            return bal
        except Exception as e:
            logger.warning("Polymarket balance check failed: %s", e)
            return None

    async def get_market_by_token(self, token_id: str) -> Optional[Dict]:
        """Fetch a single Polymarket market by token/condition ID for resolution pricing.
        Checks both active and closed markets so resolved bets get real exit prices."""
        for params in [
            {"clob_token_ids": token_id, "limit": 1},
            {"clob_token_ids": token_id, "limit": 1, "closed": "true"},
            {"clob_token_ids": token_id, "limit": 1, "active": "false"},
        ]:
            try:
                r = await self._client().get(f"{GAMMA_BASE}/markets", params=params)
                if r.status_code != 200:
                    continue
                raw = r.json()
                items = raw if isinstance(raw, list) else (raw.get("data") or raw.get("markets") or [])
                if items:
                    return self._parse_market(items[0])
            except Exception as e:
                logger.debug("get_market_by_token %s params=%s: %s", token_id[:16], params, e)
        # Fallback: try CLOB price endpoint directly
        try:
            r = await self._client().get(f"{CLOB_BASE}/last-trade-price", params={"token_id": token_id})
            if r.status_code == 200:
                data = r.json()
                price = float(data.get("price") or 0) * 100  # CLOB prices are 0-1
                if price > 0:
                    return {"yes_ask": price, "no_ask": 100 - price, "last_price": price}
        except Exception as e:
            logger.debug("CLOB price fallback %s: %s", token_id[:16], e)
        return None

    # ── Order placement (LIVE only) ────────────────────────────────────────────

    async def place_order(
        self,
        token_id:    str,
        side:        str,
        price_cents: float,
        size_usdc:   float,
    ) -> Optional[Dict]:
        """Paper mode: log and return simulated fill. Live mode: submit real order."""
        if not self.live:
            logger.info(
                "[POLY PAPER] BUY %s token=%s @ %.0f¢ $%.2f (simulated)",
                side.upper(), (token_id or "?")[:16], price_cents, size_usdc,
            )
            return {"simulated": True, "token_id": token_id, "price": price_cents}

        if not self.key_id or not self.secret_b64:
            logger.error("POLY LIVE requires POLY_API_KEY + POLY_API_SECRET in .env")
            return None

        price_frac = price_cents / 100.0
        if price_frac <= 0:
            logger.error("POLY LIVE: invalid price_cents=%s, aborting order", price_cents)
            return None

        from src.config.settings import settings as _s
        wallet = _s.polymarket.wallet_address
        if not wallet:
            logger.error("POLY LIVE: POLY_WALLET_ADDRESS not set in .env — cannot place order")
            return None

        shares     = round(size_usdc / price_frac, 2)
        expiration = int(time.time()) + 300          # order expires in 5 min if unfilled
        nonce      = int(time.time() * 1000)         # millisecond nonce

        body_dict = {
            "order": {
                "tokenID":     token_id,
                "price":       str(round(price_frac, 4)),
                "size":        str(shares),
                "side":        side.upper(),          # "BUY" or "SELL"
                "type":        "GTC",                 # Good-Till-Cancelled
                "feeRateBps":  "0",
                "nonce":       str(nonce),
                "expiration":  str(expiration),
                "maker":       wallet,
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
                logger.error(
                    "POLY LIVE order HTTP %d — %s",
                    r.status_code, r.text[:300],
                )
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

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _get_signing_key(self):
        if self._signing_key is None and self.secret_b64:
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                raw = base64.b64decode(self.secret_b64)
                self._signing_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
            except Exception as e:
                logger.error("Polymarket signing key load failed: %s", e)
        return self._signing_key

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        key = self._get_signing_key()
        if not key:
            return ""
        msg = (timestamp + method.upper() + path + body).encode()
        return base64.b64encode(key.sign(msg)).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts  = str(int(time.time() * 1000))
        sig = self._sign(ts, method, path, body)
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp":  ts,
            "X-PM-Signature":  sig,
            "Content-Type":    "application/json",
        }

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._http = None  # Force new proxy port + UA on next use
