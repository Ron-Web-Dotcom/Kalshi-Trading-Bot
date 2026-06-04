"""
Polymarket client — market data via public Gamma API, orders via CLOB API.

Market data requires NO authentication (public endpoints).
Order placement requires API key + secret (only used when POLY_LIVE_TRADING=true).
"""

import base64
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.polymarket_client")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
_TIMEOUT   = httpx.Timeout(20.0)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)",
    "Accept": "application/json",
}


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
            self._http = httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS)
        return self._http

    # ── Market data (PUBLIC — no auth) ────────────────────────────────────────

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts  = str(int(time.time() * 1000))
        sig = self._sign(ts, method, path, body)
        return {
            "X-PM-Access-Key":  self.key_id,
            "X-PM-Timestamp":   ts,
            "X-PM-Signature":   sig,
            "Content-Type":     "application/json",
        }

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 500) -> List[Dict]:
        """
        Fetch active Polymarket markets from the public Gamma API.
        Returns list of normalised market dicts ready for the opportunity hunter.
        """
        logger.info("Polymarket: fetching markets from Gamma API (limit=%d)...", limit)
        try:
            r = await self._client().get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": limit},
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

            # Fallback to bestBid/bestAsk
            if yes_price == 0:
                bid = m.get("bestBid") or 0
                ask = m.get("bestAsk") or 0
                val = float(ask or bid or 0)
                yes_price = val * 100 if val <= 1.0 else val
                no_price  = 100 - yes_price

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
                "close_time":    m.get("endDate") or m.get("endDateIso", ""),
                "open_interest": 0,
                "status":        "open",
                "_yes_token":    token_ids[0] if len(token_ids) > 0 else None,
                "_no_token":     token_ids[1] if len(token_ids) > 1 else None,
            }
        except Exception as e:
            logger.debug("Polymarket parse error: %s — %s", e, str(m)[:100])
            return None

    async def get_live_markets(self, max_hours: float = 6.0, max_markets: int = 60) -> List[Dict]:
        """
        Fetch ALL active Polymarket markets closing within max_hours — every category.
        Covers sports, crypto, politics, weather, economics, pop culture, and anything else
        that happens to resolve soon.  No category filter — time is the only gate.
        """
        import datetime as _dt
        try:
            r = await self._client().get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": 500},
            )
            if r.status_code != 200:
                return []
            raw = r.json()
            items = raw if isinstance(raw, list) else raw.get("data") or raw.get("markets") or []

            now  = _dt.datetime.now(_dt.timezone.utc)
            live = []
            for m in items:
                end_date = m.get("endDate") or m.get("endDateIso") or ""
                try:
                    close_dt = _dt.datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                    if close_dt.tzinfo is None:
                        close_dt = close_dt.replace(tzinfo=_dt.timezone.utc)
                    hours_left = (close_dt - now).total_seconds() / 3600
                    if not (0 < hours_left <= max_hours):
                        continue
                except Exception:
                    continue

                parsed = self._parse_market(m)
                if parsed:
                    parsed["is_live"] = True
                    parsed["hours_to_close"] = round(hours_left, 2)
                    live.append(parsed)

            logger.info(
                "Polymarket live markets (all categories, closing ≤%.0fh): %d found",
                max_hours, len(live),
            )
            return live[:max_markets]
        except Exception as e:
            logger.warning("Failed to fetch Polymarket live markets: %s", e)
            return []

    # ── Balance check ──────────────────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Fetch USDC balance from CLOB API (requires valid credentials)."""
        if not self.key_id:
            return None
        try:
            path = "/balance"
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                headers=self._auth_headers("GET", path),
            )
            r.raise_for_status()
            return float(r.json().get("balance", 0))
        except Exception as e:
            logger.debug("Polymarket balance check failed: %s", e)
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
        shares     = round(size_usdc / price_frac, 2) if price_frac > 0 else 0
        body_dict  = {
            "order": {
                "tokenID": token_id,
                "price":   str(round(price_frac, 4)),
                "size":    str(shares),
                "side":    side.lower(),
                "type":    "GTC",
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
            r.raise_for_status()
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
