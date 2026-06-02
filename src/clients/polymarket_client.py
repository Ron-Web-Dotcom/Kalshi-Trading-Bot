"""
Polymarket CLOB trading client — Ed25519 authentication.

Authentication (from Polymarket docs):
  Sign: Ed25519(secret_key, timestamp + method + path + body)
  Headers: X-PM-Access-Key, X-PM-Timestamp, X-PM-Signature (base64)

The secret key is a base64-encoded Ed25519 private key (32 bytes).
cryptography library (already in requirements) handles signing.
"""

import base64
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.polymarket_client")

CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
_TIMEOUT   = httpx.Timeout(15.0)


class PolymarketTradingClient:
    """
    Full Polymarket CLOB client with Ed25519 request signing.

    Paper mode (default): simulates orders, records to local DB.
    Live mode: signs and submits real CLOB orders on Polygon.
    """

    def __init__(self):
        from src.config.settings import settings
        cfg             = settings.polymarket
        self.key_id     = cfg.api_key        # the UUID (KEY ID)
        self.secret_b64 = cfg.api_secret     # base64 Ed25519 private key
        self.live       = cfg.live_trading_enabled
        self._http: Optional[httpx.AsyncClient] = None
        self._signing_key = None             # lazy-loaded Ed25519PrivateKey

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        return self._http

    def _get_signing_key(self):
        """Lazy-load and cache the Ed25519 private key."""
        if self._signing_key is None and self.secret_b64:
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
                raw = base64.b64decode(self.secret_b64)
                # Ed25519 private key is 32 bytes; if 64 bytes it's seed+public, take first 32
                self._signing_key = Ed25519PrivateKey.from_private_bytes(raw[:32])
            except Exception as e:
                logger.error("Failed to load Polymarket signing key: %s", e)
        return self._signing_key

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Ed25519 sign → base64 encoded signature."""
        key = self._get_signing_key()
        if not key:
            return ""
        msg = (timestamp + method.upper() + path + body).encode()
        sig = key.sign(msg)
        return base64.b64encode(sig).decode()

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

    async def get_markets(self, limit: int = 300) -> List[Dict]:
        """Fetch active Polymarket markets with current YES/NO prices."""
        try:
            r = await self._client().get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "closed": "false", "limit": limit},
            )
            r.raise_for_status()
            raw = r.json()
            raw = raw if isinstance(raw, list) else raw.get("data", [])

            markets = []
            for m in raw:
                prices = m.get("outcomePrices") or []
                if len(prices) < 2:
                    continue
                try:
                    yes_price = float(prices[0]) * 100
                    no_price  = float(prices[1]) * 100
                except (TypeError, ValueError):
                    continue
                if not (5 < yes_price < 95):
                    continue

                token_ids = m.get("clobTokenIds") or []
                markets.append({
                    "platform":    "polymarket",
                    "ticker":      m.get("conditionId") or m.get("id", ""),
                    "slug":        m.get("slug", ""),
                    "title":       m.get("question", ""),
                    "category":    (m.get("category") or "").lower(),
                    "yes_ask":     yes_price,
                    "no_ask":      no_price,
                    "yes_bid":     max(yes_price - 1, 1),
                    "no_bid":      max(no_price  - 1, 1),
                    "volume":      float(m.get("volume") or 0),
                    "close_time":  m.get("endDate", ""),
                    "open_interest": 0,
                    "status":      "open",
                    "_yes_token":  token_ids[0] if len(token_ids) > 0 else None,
                    "_no_token":   token_ids[1] if len(token_ids) > 1 else None,
                })
            logger.info("Polymarket: fetched %d tradeable markets (raw=%d)", len(markets), len(raw))
            return markets
        except Exception as e:
            logger.warning("Polymarket market fetch failed: %s", e)
            return []

    async def get_balance(self) -> Optional[float]:
        """Fetch USDC balance (requires valid credentials)."""
        if not self.key_id:
            return None
        path = "/balance"
        try:
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                headers=self._auth_headers("GET", path),
            )
            r.raise_for_status()
            return float(r.json().get("balance", 0))
        except Exception as e:
            logger.debug("Polymarket balance fetch failed: %s", e)
            return None

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_order(
        self,
        token_id:    str,
        side:        str,
        price_cents: float,
        size_usdc:   float,
    ) -> Optional[Dict]:
        """Place a limit order. Paper mode logs and returns simulated response."""
        if not self.live:
            logger.debug(
                "[POLY PAPER] %s token=%s price=%.0f¢ size=$%.2f",
                side.upper(), (token_id or "?")[:12], price_cents, size_usdc,
            )
            return {"simulated": True, "token_id": token_id, "price": price_cents}

        if not self.key_id or not self.secret_b64:
            logger.error("Polymarket live trading requires POLY_API_KEY and POLY_API_SECRET in .env")
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
                "POLY ORDER: token=%s %s @ %.0f¢ $%.2f → id=%s",
                (token_id or "?")[:12], side.upper(),
                price_cents, size_usdc, resp.get("orderID", "?"),
            )
            return resp
        except Exception as e:
            logger.error("Polymarket order failed: %s", e)
            return None

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
