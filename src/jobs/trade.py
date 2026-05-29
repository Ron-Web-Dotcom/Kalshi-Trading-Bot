"""Job: execute (paper or live) trades based on AI decisions and arb signals."""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("trading.jobs.trade")

MAX_TRADES_PER_CYCLE = 3
MAX_MARKETS_TO_SCAN = 20
MIN_VOLUME = 50


@dataclass
class TradingResults:
    total_positions: int = 0
    total_capital_used: float = 0.0
    capital_efficiency: float = 0.0
    expected_annual_return: float = 0.0
    arb_trades: int = 0
    ai_trades: int = 0


async def run_trading_job(db=None) -> TradingResults:
    """
    One full trading cycle:
      1. Load cached markets from DB
      2. Compare vs Polymarket → cross-market arb signals
      3. Detect internal arb (YES+NO < 100)
      4. Execute arb signals directly (pure math, no AI gate needed)
      5. AI decisions on remaining top markets
      6. Execute AI trades through risk gate
    """
    from src.config.settings import settings
    from src.data.market_data import MarketDataFetcher
    from src.data.external_markets import ExternalMarketComparator
    from src.strategy.arbitrage import ArbitrageDetector
    from src.jobs.decide import make_decision_for_market
    from src.execution.paper_trader import PaperTrader
    from src.execution.live_trader import LiveTrader
    from src.risk.manager import RiskManager
    from src.risk.scaling import AutoScaler
    from src.alerts.discord import DiscordAlerter
    from src.clients.kalshi_client import KalshiClient
    from src.utils.database import DatabaseManager

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    live_mode = settings.trading.live_trading_enabled
    kalshi = KalshiClient()
    fetcher = MarketDataFetcher(kalshi, db)
    comparator = ExternalMarketComparator(db)
    arb_detector = ArbitrageDetector()
    risk = RiskManager(db)
    scaler = AutoScaler()
    discord = DiscordAlerter()

    if live_mode:
        trader = LiveTrader(kalshi=kalshi, db=db, discord=discord, scaler=scaler, risk=risk)
    else:
        from src.execution.paper_trader import PaperTrader
        trader = PaperTrader(db=db, discord=discord, scaler=scaler, risk=risk)

    results = TradingResults()
    trades_this_cycle = 0

    try:
        # ── 1. Load markets ───────────────────────────────────────────────────
        markets = await fetcher.get_cached_markets(min_volume=MIN_VOLUME)
        if not markets:
            logger.info("No markets in DB yet — skipping trade cycle")
            return results

        market_map = {m["ticker"]: m for m in markets}

        # ── 2 & 3. Arbitrage detection ────────────────────────────────────────
        ext_comparisons = await comparator.compare_and_log(markets)
        cross_signals = arb_detector.detect(ext_comparisons)
        internal_signals = arb_detector.detect_internal(markets)
        all_signals = cross_signals + internal_signals

        if all_signals:
            logger.info(
                f"Arb signals: {len(cross_signals)} cross-market, "
                f"{len(internal_signals)} internal"
            )

        # ── 4. Execute arb signals directly (math guarantees edge) ────────────
        for sig in all_signals:
            if trades_this_cycle >= MAX_TRADES_PER_CYCLE:
                break

            ticker = sig["ticker"]
            market = market_map.get(ticker)
            if not market:
                continue

            sig_type = sig["signal_source"]

            if sig_type == "internal_arb":
                # Buy both YES and NO legs — risk-free profit
                yes_price = sig["yes_price"]
                no_price = sig["no_price"]

                for side, price in [("yes", yes_price), ("no", no_price)]:
                    allowed, reason = risk.check_trade(
                        ticker + f"_{side}", scaler.current_size,
                        current_positions=[], portfolio_value=1000.0
                    )
                    if not allowed:
                        logger.info(f"Internal arb {side} leg blocked [{ticker}]: {reason}")
                        continue
                    rec = await trader.execute(
                        ticker=ticker,
                        action="BUY",
                        side=side,
                        price_cents=price,
                        ai_confidence=99.0,
                        ai_reasoning=f"Internal arb: YES+NO={yes_price+no_price:.0f}¢, net edge={sig['edge_cents']:.1f}¢",
                        signal_source="internal_arb",
                    )
                    if rec:
                        trades_this_cycle += 1
                        results.total_positions += 1
                        results.total_capital_used += rec.get("total_cost", 0)
                        results.arb_trades += 1

            else:
                # Cross-market: single side already determined by detector
                side = sig.get("side", "yes")
                price = market.get(f"{side}_ask", 0)
                if price <= 0 or price >= 100:
                    continue

                allowed, reason = risk.check_trade(
                    ticker, scaler.current_size,
                    current_positions=[], portfolio_value=1000.0
                )
                if not allowed:
                    logger.info(f"Cross-market arb blocked [{ticker}]: {reason}")
                    continue

                rec = await trader.execute(
                    ticker=ticker,
                    action="BUY",
                    side=side,
                    price_cents=price,
                    ai_confidence=95.0,
                    ai_reasoning=(
                        f"Cross-market arb: Kalshi={sig['kalshi_price']:.0f}¢ "
                        f"Poly={sig['poly_price']:.0f}¢ net_edge={sig['edge_cents']:.1f}¢"
                    ),
                    signal_source="cross_market_arb",
                )
                if rec:
                    trades_this_cycle += 1
                    results.total_positions += 1
                    results.total_capital_used += rec.get("total_cost", 0)
                    results.arb_trades += 1

        # ── 5. AI decisions on top non-arb markets ────────────────────────────
        arb_tickers = {s["ticker"] for s in all_signals}
        scored = sorted(
            [m for m in markets if m.get("ticker") not in arb_tickers],
            key=lambda m: m.get("volume", 0),
            reverse=True,
        )

        for market in scored[:MAX_MARKETS_TO_SCAN]:
            if trades_this_cycle >= MAX_TRADES_PER_CYCLE:
                break

            decision = await make_decision_for_market(market, all_signals, db=db)
            if not decision:
                continue

            ticker = decision["ticker"]
            side = "yes"  # AI BUY means YES side
            price = market.get("yes_ask", 50)
            if price <= 0 or price >= 100:
                continue

            allowed, reason = risk.check_trade(
                ticker, scaler.current_size,
                current_positions=[],
                portfolio_value=1000.0,
            )
            if not allowed:
                logger.info(f"AI trade blocked [{ticker}]: {reason}")
                continue

            rec = await trader.execute(
                ticker=ticker,
                action=decision["action"],
                side=side,
                price_cents=price,
                ai_confidence=decision["confidence"],
                ai_reasoning=decision["reasoning"],
                signal_source=decision.get("model", "ai"),
            )
            if rec:
                trades_this_cycle += 1
                results.total_positions += 1
                results.total_capital_used += rec.get("total_cost", 0)
                results.ai_trades += 1

    except Exception as e:
        logger.error(f"Trade job error: {e}", exc_info=True)
        try:
            await discord.error_alert(str(e), context="run_trading_job")
        except Exception:
            pass
    finally:
        await kalshi.close()
        await comparator.close()

    if results.total_capital_used > 0:
        results.capital_efficiency = min(results.total_capital_used / 1000.0, 1.0)

    if results.total_positions > 0:
        logger.info(
            f"Cycle done: {results.total_positions} trades "
            f"({results.arb_trades} arb, {results.ai_trades} AI) | "
            f"Capital=${results.total_capital_used:.2f}"
        )

    return results
