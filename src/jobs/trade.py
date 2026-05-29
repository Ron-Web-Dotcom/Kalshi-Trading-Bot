"""Job: execute (paper) trades based on decisions."""

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("trading.jobs.trade")


@dataclass
class TradingResults:
    total_positions: int = 0
    total_capital_used: float = 0.0
    capital_efficiency: float = 0.0
    expected_annual_return: float = 0.0


async def run_trading_job(db=None) -> TradingResults:
    """Run one trading cycle: scan markets → detect signals → AI decide → paper trade."""
    from src.config.settings import settings
    from src.data.market_data import MarketDataFetcher
    from src.data.external_markets import ExternalMarketComparator
    from src.strategy.arbitrage import ArbitrageDetector
    from src.jobs.decide import make_decision_for_market
    from src.execution.paper_trader import PaperTrader
    from src.risk.manager import RiskManager
    from src.risk.scaling import AutoScaler
    from src.alerts.discord import DiscordAlerter
    from src.clients.kalshi_client import KalshiClient
    from src.utils.database import DatabaseManager

    if db is None:
        db = DatabaseManager()
        await db.initialize()

    kalshi = KalshiClient()
    fetcher = MarketDataFetcher(kalshi, db)
    comparator = ExternalMarketComparator(db)
    arb_detector = ArbitrageDetector()
    risk = RiskManager(db)
    scaler = AutoScaler()
    discord = DiscordAlerter()
    trader = PaperTrader(db=db, discord=discord, scaler=scaler, risk=risk)

    results = TradingResults()

    try:
        # Load cached markets from DB (already ingested)
        markets = await fetcher.get_cached_markets(min_volume=50)
        if not markets:
            logger.info("No markets in DB yet — skipping trade cycle")
            return results

        # External comparisons for arbitrage
        ext_comparisons = await comparator.compare_and_log(markets)
        arb_signals = arb_detector.detect(ext_comparisons)
        internal_signals = arb_detector.detect_internal(markets)
        all_signals = arb_signals + internal_signals

        if all_signals:
            logger.info(f"Found {len(all_signals)} arbitrage signal(s)")

        # AI decisions on top markets with arb signals first
        priority_tickers = {s["ticker"] for s in all_signals}
        scored = sorted(
            markets,
            key=lambda m: (m.get("ticker", "") in priority_tickers, m.get("volume", 0)),
            reverse=True
        )

        trades_this_cycle = 0
        max_trades_per_cycle = settings.trading.avoid_overtrading_minutes  # reuse as soft cap

        for market in scored[:20]:  # Check top 20 markets
            if trades_this_cycle >= 3:  # Max 3 trades per cycle
                break

            decision = await make_decision_for_market(market, all_signals, db=db)
            if not decision:
                continue

            ticker = decision["ticker"]
            side = "yes"  # default to YES side for BUY signals
            price = market.get("yes_ask", 50) if side == "yes" else market.get("no_ask", 50)
            if price <= 0 or price >= 100:
                continue

            allowed, reason = risk.check_trade(
                ticker, scaler.current_size,
                current_positions=[],
                portfolio_value=1000.0
            )
            if not allowed:
                logger.info(f"Trade blocked [{ticker}]: {reason}")
                continue

            record = await trader.execute(
                ticker=ticker,
                action=decision["action"],
                side=side,
                price_cents=price,
                ai_confidence=decision["confidence"],
                ai_reasoning=decision["reasoning"],
                signal_source=decision.get("model", "ai"),
            )

            if record:
                trades_this_cycle += 1
                results.total_positions += 1
                results.total_capital_used += record.get("total_cost", 0)

    except Exception as e:
        logger.error(f"Trade job error: {e}")
        try:
            await discord.error_alert(str(e), context="run_trading_job")
        except Exception:
            pass
    finally:
        await kalshi.close()
        await comparator.close()

    if results.total_capital_used > 0:
        results.capital_efficiency = min(results.total_capital_used / 1000, 1.0)

    return results
