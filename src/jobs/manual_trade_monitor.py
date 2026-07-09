"""
Monitor for manual trades placed by the user directly on Kalshi/Polymarket.

Each cycle:
  1. Fetch live positions from Kalshi API
  2. Compare against bot's DB — anything not in DB = manual trade
  3. Run AI analysis on the manual trade
  4. Send Discord alert: AGREE or DISAGREE with reasoning
  5. If DISAGREE and confidence >= threshold, optionally close the position
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

logger = logging.getLogger("trading.manual_monitor")

_SEEN_MANUAL_TICKERS: set = set()   # in-memory — reset on restart, that's fine


async def check_manual_trades(db, discord=None) -> None:
    """Detect user-placed trades, analyze them, alert on Discord.

    Works in both live and paper mode — Kalshi's positions API returns real account
    positions regardless of the bot's trading mode.  If the user places a trade
    manually on the Kalshi app, we'll catch it here even in paper mode.
    """
    from src.config.settings import settings
    from src.clients.kalshi_client import KalshiClient

    # Require RSA key credentials — without them the positions API will 401
    if not (settings.kalshi.api_key_id and
            (settings.kalshi.private_key_pem or settings.kalshi.private_key_path)):
        return

    kalshi = KalshiClient()
    try:
        await _check_kalshi_manual_trades(kalshi, db, discord, settings)
        if settings.polymarket.enabled:
            await _check_polymarket_manual_trades(db, discord, settings)
    finally:
        await kalshi.close()


async def _check_kalshi_manual_trades(kalshi, db, discord, settings) -> None:
    from src.ai.decision import AIDecisionEngine

    try:
        resp = await kalshi._request("GET", "/portfolio/positions")
        positions = resp.get("positions") or []
    except Exception as e:
        logger.debug("Could not fetch Kalshi positions for manual trade check: %s", e)
        return

    for pos in positions:
        ticker      = pos.get("ticker", "")
        side        = "yes" if float(pos.get("position", 0) or 0) > 0 else "no"
        contracts   = abs(int(pos.get("position", 0) or 0))

        if not ticker or contracts == 0:
            continue

        # Already seen / already in bot DB?
        if ticker in _SEEN_MANUAL_TICKERS:
            continue

        existing = await db.fetchone(
            "SELECT id FROM positions WHERE ticker=? AND status='open'", (ticker,)
        )
        if existing:
            _SEEN_MANUAL_TICKERS.add(ticker)
            continue

        # This is a NEW manual trade the bot didn't place
        logger.info("Manual trade detected: %s %s x%d", ticker, side.upper(), contracts)
        _SEEN_MANUAL_TICKERS.add(ticker)

        # Fetch market data for analysis
        market = await db.fetchone(
            "SELECT * FROM markets WHERE ticker=?", (ticker,)
        )
        if not market:
            market = {"ticker": ticker}

        # Run AI analysis
        try:
            engine   = AIDecisionEngine(db=db)
            decision = await engine.decide(dict(market), [])
            ai_side  = decision.side or "yes"
            conf     = decision.confidence
            net_ev   = decision.net_ev
            reasoning = decision.reasoning

            agrees = (
                decision.action == "BUY"
                and ai_side == side
                and conf >= settings.trading.min_ai_confidence
                and (net_ev or 0) > 0
            )

            await _send_manual_trade_alert(
                discord=discord,
                ticker=ticker,
                title=(market.get("title") or ticker),
                side=side,
                contracts=contracts,
                agrees=agrees,
                ai_side=ai_side,
                confidence=conf,
                net_ev=net_ev,
                reasoning=reasoning,
                platform="Kalshi",
            )

            # Auto opt-out if bot strongly disagrees
            if not agrees and conf >= 75 and decision.action == "HOLD":
                logger.warning(
                    "Bot DISAGREES with manual trade %s — would close if live order API available",
                    ticker,
                )

        except Exception as e:
            logger.warning("AI analysis of manual trade %s failed: %s", ticker, e)


_SEEN_MANUAL_POLY_IDS: set = set()


async def _check_polymarket_manual_trades(db, discord, settings) -> None:
    """Check Polymarket CLOB portfolio for manual trades not in bot DB."""
    from src.clients.polymarket_client import PolymarketTradingClient, CLOB_BASE
    from src.ai.decision import AIDecisionEngine

    client = PolymarketTradingClient()
    if not client.key_id or not client.secret_b64:
        return

    try:
        path = "/positions"
        r = await client._client().get(
            f"{CLOB_BASE}{path}",
            headers=client._auth_headers("GET", path),
        )
        if r.status_code != 200:
            return
        data = r.json()
        positions = data if isinstance(data, list) else data.get("positions") or []
    except Exception as e:
        logger.debug("Could not fetch Polymarket positions for manual trade check: %s", e)
        return
    finally:
        try:
            await client._client().aclose()
        except Exception:
            pass

    for pos in positions:
        token_id  = pos.get("asset_id") or pos.get("token_id") or ""
        size      = float(pos.get("size") or 0)
        if not token_id or size <= 0:
            continue
        if token_id in _SEEN_MANUAL_POLY_IDS:
            continue

        existing = await db.fetchone(
            "SELECT id FROM positions WHERE ticker=? AND status='open'", (token_id,)
        )
        if existing:
            _SEEN_MANUAL_POLY_IDS.add(token_id)
            continue

        logger.info("Polymarket manual trade detected: token=%s size=%.2f", token_id[:20], size)
        _SEEN_MANUAL_POLY_IDS.add(token_id)

        # Look up market info from DB (may have been stored during market scan)
        market = await db.fetchone(
            "SELECT * FROM markets WHERE ticker=? OR ticker LIKE ?",
            (token_id, f"%{token_id[:16]}%"),
        )
        title = (market or {}).get("title") or f"Polymarket token {token_id[:16]}"

        try:
            engine   = AIDecisionEngine(db=db)
            decision = await engine.decide(dict(market or {"ticker": token_id, "title": title}), [])
            agrees = (
                decision.action == "BUY"
                and float(decision.confidence or 0) >= settings.trading.min_ai_confidence
                and (decision.net_ev or 0) > 0
            )
            await _send_manual_trade_alert(
                discord=discord,
                ticker=token_id[:20],
                title=title,
                side="yes",
                contracts=int(size),
                agrees=agrees,
                ai_side=(decision.side or "yes"),
                confidence=float(decision.confidence or 0),
                net_ev=decision.net_ev,
                reasoning=decision.reasoning,
                platform="Polymarket",
            )
        except Exception as e:
            logger.warning("AI analysis of Polymarket manual trade failed: %s", e)


async def _send_manual_trade_alert(
    discord,
    ticker: str,
    title: str,
    side: str,
    contracts: int,
    agrees: bool,
    ai_side: str,
    confidence: float,
    net_ev: Optional[float],
    reasoning: str,
    platform: str = "Kalshi",
) -> None:
    if not discord:
        return

    verdict    = "✅ AGREE" if agrees else "❌ DISAGREE"
    color      = 0x00FF7F if agrees else 0xFF4444
    ev_str     = f"{net_ev:+.1f}¢" if net_ev is not None else "unknown"
    side_upper = side.upper()
    ai_upper   = ai_side.upper()

    if agrees:
        verdict_detail = (
            f"AI also sees edge on **{side_upper}** with **{confidence:.0f}%** confidence.\n"
            f"Expected value: **{ev_str}** per contract. Good trade."
        )
    else:
        verdict_detail = (
            f"AI recommends **{ai_upper}** (or HOLD) at **{confidence:.0f}%** confidence.\n"
            f"EV: **{ev_str}**. Consider reviewing this position.\n"
            f"_{reasoning[:200]}_"
        )

    payload = {
        "embeds": [{
            "title": f"👤 Manual Trade Detected — {verdict}",
            "description": (
                f"You placed a trade the bot didn't make.\n"
                f"**{title[:80]}**"
            ),
            "color": color,
            "fields": [
                {"name": "🏦 Platform",    "value": platform,          "inline": True},
                {"name": "📌 Ticker",      "value": ticker,            "inline": True},
                {"name": "🎯 Your Bet",    "value": f"BUY {side_upper} × {contracts}", "inline": True},
                {"name": "🤖 AI Verdict",  "value": verdict,           "inline": True},
                {"name": "📊 Analysis",    "value": verdict_detail,    "inline": False},
            ],
            "timestamp": datetime.now(_ET).isoformat(),
            "footer": {"text": "Holding with you 🤝" if agrees else "Consider reviewing this position"},
        }]
    }
    await discord._post(payload)
