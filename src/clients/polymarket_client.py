"""
Polymarket CLOB trading client — L2 authentication (API key + secret + passphrase).

Supports:
  - Reading market data (already in external_markets.py)
  - Paper trading simulation (tracks positions in local DB, no real orders)
  - Live trading via signed CLOB API orders (requires POLY_API_KEY etc. in .env)

Authentication:
  L2 auth signs requests with HMAC-SHA256:
  signature = HMAC_SHA256(api_secret, timestamp + method + path + body)
  Headers: POLY-API-KEY, POLY-API-SIGNATURE, POLY-API-PASSPHRASE, POLY-API-TIMESTAMP

Polymarket price convention:
  Prices are fractions 0.0–1.0 on the wire (e.g. 0.45 = 45¢ equivalent).
  We convert to cents internally: price_cents = price_fraction * 100.
  Min order size: $1 USDC (on Polygon).
  Polymarket fee: ~2% on winnings (not on notional), built into price spread.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("trading.polymarket_client")

CLOB_BASE   = "https://clob.polymarket.com"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
_TIMEOUT    = httpx.Timeout(15.0)


class PolymarketTradingClient:
    """
    Full Polymarket CLOB client.

    Paper mode (default): simulates orders, records positions in local DB.
    Live mode: signs and submits real CLOB orders on Polygon.
    """

    def __init__(self):
        from src.config.settings import settings
        cfg              = settings.polymarket
        self.api_key     = cfg.api_key
        self.api_secret  = cfg.api_secret
        self.passphrase  = cfg.passphrase
        self.live        = cfg.live_trading_enabled
        self._http: Optional[httpx.AsyncClient] = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        return self._http

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """HMAC-SHA256 signature for L2 auth."""
        msg = timestamp + method.upper() + path + body
        return hmac.new(
            self.api_secret.encode(),
            msg.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        return {
            "POLY-API-KEY":        self.api_key,
            "POLY-API-SIGNATURE":  self._sign(ts, method, path, body),
            "POLY-API-PASSPHRASE": self.passphrase,
            "POLY-API-TIMESTAMP":  ts,
            "Content-Type":        "application/json",
        }

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_markets(self, limit: int = 300) -> List[Dict]:
        """
        Fetch active Polymarket markets with current YES/NO prices.
        Returns normalised dicts matching our internal format.
        """
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
                    yes_price = float(prices[0]) * 100   # fraction → cents
                    no_price  = float(prices[1]) * 100
                except (TypeError, ValueError):
                    continue
                if not (5 < yes_price < 95):
                    continue

                markets.append({
                    "platform":    "polymarket",
                    "ticker":      m.get("conditionId") or m.get("id", ""),
                    "slug":        m.get("slug", ""),
                    "title":       m.get("question", ""),
                    "category":    (m.get("category") or "").lower(),
                    "yes_ask":     yes_price,
                    "no_ask":      no_price,
                    "yes_bid":     yes_price - 1,   # approximate; Gamma doesn't give bid
                    "no_bid":      no_price  - 1,
                    "volume":      float(m.get("volume") or 0),
                    "end_date":    m.get("endDate", ""),
                    "close_time":  m.get("endDate", ""),
                    "open_interest": 0,
                    "status":      "open",
                    # Token IDs needed for order placement
                    "_yes_token":  m.get("clobTokenIds", [None])[0] if m.get("clobTokenIds") else None,
                    "_no_token":   m.get("clobTokenIds", [None, None])[1] if m.get("clobTokenIds") and len(m.get("clobTokenIds", [])) > 1 else None,
                })
            logger.debug("Polymarket: fetched %d tradeable markets", len(markets))
            return markets
        except Exception as e:
            logger.warning("Polymarket market fetch failed: %s", e)
            return []

    async def get_balance(self) -> Optional[float]:
        """Fetch USDC balance from Polymarket (requires auth)."""
        if not self.api_key:
            return None
        path = "/balance"
        try:
            r = await self._client().get(
                f"{CLOB_BASE}{path}",
                headers=self._auth_headers("GET", path),
            )
            r.raise_for_status()
            data = r.json()
            return float(data.get("balance", 0))
        except Exception as e:
            logger.debug("Polymarket balance fetch failed: %s", e)
            return None

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_order(
        self,
        token_id:    str,
        side:        str,     # "buy" or "sell"
        price_cents: float,   # 0–100¢
        size_usdc:   float,   # dollar amount to spend
    ) -> Optional[Dict]:
        """
        Place a limit order on Polymarket CLOB.
        Returns order response or None on failure.

        price_cents is converted back to fraction for the wire format.
        size = size_usdc / price_fraction  → number of shares/contracts.
        """
        if not self.live:
            logger.debug(
                "[POLY PAPER] Would place %s order: token=%s price=%.0f¢ size=$%.2f",
                side.upper(), token_id[:12] if token_id else "?", price_cents, size_usdc,
            )
            return {"simulated": True, "token_id": token_id, "price": price_cents, "size": size_usdc}

        if not self.api_key or not self.api_secret:
            logger.error("Polymarket live trading requires POLY_API_KEY and POLY_API_SECRET")
            return None

        price_frac = price_cents / 100.0
        shares     = round(size_usdc / price_frac, 2) if price_frac > 0 else 0

        body_dict = {
            "order": {
                "tokenID":  token_id,
                "price":    str(round(price_frac, 4)),
                "size":     str(shares),
                "side":     side.lower(),
                "type":     "GTC",   # Good-Till-Cancelled limit order
                "feeRateBps": "0",
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
                "POLY ORDER placed: token=%s %s @ %.0f¢ $%.2f → order_id=%s",
                token_id[:12] if token_id else "?",
                side.upper(), price_cents, size_usdc,
                resp.get("orderID", "?"),
            )
            return resp
        except Exception as e:
            logger.error("Polymarket order failed: %s", e)
            return None

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
