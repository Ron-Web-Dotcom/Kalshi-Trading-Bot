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
from typing import Dict, List, Optional

logger = logging.getLogger("trading.manual_monitor")

_SEEN_MANUAL_TICKERS: set = set()   # in-memory — reset on restart, that's fine


async def check_manual_trades(db, discord=None) -> None:
    """Detect user-placed trades, analyze them, alert on Discord."""
    from src.config.settings import settings
    from src.clients.kalshi_client import KalshiClient

    if not settings.trading.live_trading_enabled:
        # In paper mode we can't fetch real positions — skip
        return

    kalshi = KalshiClient()
    try:
        await _check_kalshi_manual_trades(kalshi, db, discord, settings)
    finally:
        await kalshi.close()


async def _check_kalshi_manual_trades(kalshi, db, discord, settings) -> None:
    from src.ai.decision import AIDecisionEngine

    try:
        resp = await kalshi.request("GET", "/portfolio/positions")
        positions = resp.get("positions") or []
    except Exception as e:
        logger.warning("Could not fetch Kalshi positions for manual trade check: %s", e)
        return

    for pos in positions:
        ticker      = pos.get("ticker", "")
        side        = "yes" if (pos.get("position", 0) or 0) > 0 else "no"
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
            )

            # Auto opt-out if bot strongly disagrees
            if not agrees and conf >= 75 and decision.action == "HOLD":
                logger.warning(
                    "Bot DISAGREES with manual trade %s — would close if live order API available",
                    ticker,
                )

        except Exception as e:
            logger.warning("AI analysis of manual trade %s failed: %s", ticker, e)


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
                {"name": "📌 Ticker",      "value": ticker,            "inline": True},
                {"name": "🎯 Your Bet",    "value": f"BUY {side_upper} × {contracts}", "inline": True},
                {"name": "🤖 AI Verdict",  "value": verdict,           "inline": True},
                {"name": "📊 Analysis",    "value": verdict_detail,    "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Reply with !close <ticker> to exit the trade" if not agrees else "Holding with you 🤝"},
        }]
    }
    await discord._post(payload)
