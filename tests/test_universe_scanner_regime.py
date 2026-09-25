from datetime import timedelta
from decimal import Decimal

import numpy as np
import pytest
from market_fixtures import (
    FakeUniverseProvider,
    instrument,
    liquid,
    oscillating,
    random_walk,
    store,
)
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.events import EventBus
from app.global_services.regime import RegimeClassifier, RegimeService
from app.global_services.scanner import MarketScanner, SignalDetector
from app.global_services.universe import BinanceUniverseProvider, EligibilityEngine, UniverseService
from app.models import (
    AssetEligibility,
    AssetInstrument,
    MarketRegimeRecord,
    Opportunity,
    SystemEvent,
)

EXCHANGE_INFO = {"symbols": [
    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.00010000", "minQty": "0.00010000"},
                 {"filterType": "NOTIONAL", "minNotional": "5.00000000"}]},
    {"symbol": "LUNAUSDT", "baseAsset": "LUNA", "quoteAsset": "USDT", "status": "BREAK", "filters": []},
    {"symbol": "broken"},
]}


def test_binance_exchange_info_and_ticker_parsing():
    parsed = BinanceUniverseProvider.parse_exchange_info(EXCHANGE_INFO)
    assert [item.symbol for item in parsed] == ["ETHUSDT", "LUNAUSDT"]
    eth = parsed[0]
    assert eth.tick_size == Decimal("0.01") and eth.step_size == Decimal("0.0001") and eth.min_notional == Decimal("5")
    stats = BinanceUniverseProvider.parse_stats(
        [{"symbol": "ETHUSDT", "lastPrice": "3000.5", "quoteVolume": "123456789", "priceChangePercent": "-2.1", "count": 9},
         {"symbol": "BAD"}],
        [{"symbol": "ETHUSDT", "bidPrice": "3000.4", "askPrice": "3000.6"}],
    )
    assert set(stats) == {"ETHUSDT"} and round(stats["ETHUSDT"].spread_bps, 3) == 0.667


def test_eligibility_rules_explain_every_exclusion():
    engine = EligibilityEngine(Settings(universe_include_symbols="FORCEUSDT"))
    good = engine.evaluate(instrument("SOLUSDT", "SOL"), liquid("SOLUSDT"), history_candles=500,
                           recent_quality_failures=0, volatility=0.01)
    assert good.eligible and good.reasons == []
    cases = {
        "NOT_TRADING": (instrument("OLDUSDT", "OLD", status="BREAK"), liquid("OLDUSDT")),
        "UNSUPPORTED_QUOTE": (instrument("SOLBTC", "SOL", quote="BTC"), liquid("SOLBTC")),
        "EXCLUDED_BASE_ASSET": (instrument("USDCUSDT", "USDC"), liquid("USDCUSDT")),
        "LEVERAGED_TOKEN": (instrument("BTCDOWNUSDT", "BTCDOWN"), liquid("BTCDOWNUSDT")),
        "ILLIQUID": (instrument("TINYUSDT", "TINY"), liquid("TINYUSDT", volume=1000)),
        "ABNORMAL_MARKET_MOVE": (instrument("PUMPUSDT", "PUMP"), liquid("PUMPUSDT", change=85)),
        "NO_MARKET_STATISTICS": (instrument("GHOSTUSDT", "GHOST"), None),
    }
    for reason, (info, stats) in cases.items():
        verdict = engine.evaluate(info, stats, history_candles=500, recent_quality_failures=0, volatility=None)
        assert not verdict.eligible and reason in verdict.reasons, reason
    wide = liquid("WIDEUSDT")
    wide.ask = wide.bid * 1.01
    assert "SPREAD_TOO_WIDE" in engine.evaluate(instrument("WIDEUSDT", "WIDE"), wide, history_candles=500,
                                                recent_quality_failures=0, volatility=None).reasons
    broken = engine.evaluate(instrument("DATAUSDT", "DATA"), liquid("DATAUSDT"), history_candles=10,
                             recent_quality_failures=3, volatility=None)
    assert "RECENT_DATA_QUALITY_FAILURES" in broken.reasons and broken.metrics["history_status"] == "INSUFFICIENT_HISTORY"
    forced = engine.evaluate(instrument("FORCEUSDT", "FORCE"), liquid("FORCEUSDT", volume=10), history_candles=0,
                             recent_quality_failures=0, volatility=None)
    assert forced.eligible and forced.metrics["forced_include"]


@pytest.mark.asyncio
async def test_universe_refresh_is_dynamic_multi_asset_and_mirrors_paper_constraints(session):
    provider = FakeUniverseProvider(
        [instrument("SOLUSDT", "SOL"), instrument("ETHUSDT", "ETH"), instrument("XRPUSDT", "XRP"),
         instrument("DEADUSDT", "DEAD", status="BREAK"), instrument("ETHBTC", "ETH", quote="BTC")],
        {"SOLUSDT": liquid("SOLUSDT", volume=9e8), "ETHUSDT": liquid("ETHUSDT", volume=2e9),
         "XRPUSDT": liquid("XRPUSDT", volume=5e8), "DEADUSDT": liquid("DEADUSDT")},
    )
    result = await UniverseService(session, Settings(universe_max_assets=2), provider).refresh()
    assert result["eligible"] == ["ETHUSDT", "SOLUSDT"]  # ranked by liquidity, capped at 2
    rows = session.scalars(select(AssetEligibility).where(AssetEligibility.run_id == result["run_id"])).all()
    by_symbol = {row.symbol: row for row in rows}
    assert "ETHBTC" not in by_symbol  # irrelevant quotes are not persisted
    assert "OUTSIDE_MAX_UNIVERSE_SIZE" in by_symbol["XRPUSDT"].reasons
    assert "NOT_TRADING" in by_symbol["DEADUSDT"].reasons
    paper = session.scalar(select(AssetInstrument).where(AssetInstrument.exchange == "paper", AssetInstrument.exchange_symbol == "SOLUSDT"))
    assert paper is not None and paper.tick_size == Decimal("0.01") and paper.metadata_payload["mirrored"]
    assert UniverseService.eligible_symbols(session, "binance") == ["ETHUSDT", "SOLUSDT"]


def test_regime_classification_and_uncertainty():
    classifier = RegimeClassifier()
    assert classifier.classify(oscillating(30)).regime == "UNKNOWN"
    up = oscillating(300, amplitude=0.0, drift=0.004, noise=0.002)
    result = classifier.classify(up)
    assert result.regime == "TRENDING_UP" and result.confidence >= 0.5 and "efficiency" in result.evidence
    down = oscillating(300, amplitude=0.0, drift=-0.004, noise=0.002)
    assert classifier.classify(down).regime == "TRENDING_DOWN"
    crash = oscillating(300, amplitude=0.0, noise=0.004, seed=5)
    crash.loc[crash.index[-5:], "close"] = crash["close"].iloc[-6] * np.array([0.97, 0.93, 0.88, 0.83, 0.78])
    crash["low"] = crash[["open", "close"]].min(axis=1) * 0.999
    assert classifier.classify(crash).regime in {"PANIC", "HIGH_VOLATILITY", "BREAKOUT"}


def test_regime_series_has_no_look_ahead():
    frame = oscillating(200)
    full = RegimeClassifier().classify_series(frame)
    truncated = RegimeClassifier().classify_series(frame.iloc[:150])
    assert list(full.iloc[:150]) == list(truncated)


def test_regime_transitions_are_persisted_once_per_candle(session):
    frame = oscillating(300, amplitude=0.0, drift=0.004, noise=0.002)
    service = RegimeService(session)
    first, _ = service.update("binance", "SOLUSDT", "1h", frame.iloc[:200])
    again, _ = service.update("binance", "SOLUSDT", "1h", frame.iloc[:200])
    assert first is not None and again.id == first.id
    falling = frame.copy()
    falling.loc[falling.index[200:], "close"] = falling["close"].iloc[199] * np.exp(-0.006 * np.arange(1, 101))
    falling["open"] = falling["close"].shift(1).fillna(falling["close"])
    falling["high"] = falling[["open", "close"]].max(axis=1) * 1.001
    falling["low"] = falling[["open", "close"]].min(axis=1) * 0.999
    latest, result = service.update("binance", "SOLUSDT", "1h", falling)
    assert latest.previous_regime == first.regime
    assert latest.is_transition == (latest.regime != first.regime)
    assert session.scalar(select(func.count()).select_from(MarketRegimeRecord)) == 2


def test_signal_detectors_volume_volatility_breakout():
    frame = random_walk(200, seed=11)
    detector = SignalDetector()
    frame.loc[frame.index[-1], "volume"] = frame["volume"].median() * 6
    codes = {signal.code for signal in detector.detect(frame)}
    assert "VOLUME_SPIKE" in codes
    breakout = random_walk(200, seed=12)
    top = breakout["high"].iloc[-21:-1].max()
    breakout.loc[breakout.index[-1], ["close", "high"]] = [top * 1.02, top * 1.03]
    assert "BREAKOUT_UP" in {signal.code for signal in detector.detect(breakout)}
    volatile = random_walk(200, seed=13)
    shocks = volatile["close"].iloc[-10:].to_numpy() * np.array([1.04, 0.95, 1.05, 0.94, 1.06, 0.95, 1.05, 0.94, 1.05, 0.96])
    volatile.loc[volatile.index[-10:], "close"] = shocks
    assert "VOLATILITY_EXPANSION" in {signal.code for signal in detector.detect(volatile)}
    assert detector.detect(frame.iloc[:30]) == []


@pytest.mark.asyncio
async def test_scanner_creates_ranked_deduplicated_opportunities_without_trading(session):
    frame = random_walk(200, seed=21)
    frame.loc[frame.index[-1], "volume"] = frame["volume"].median() * 8
    store(session, frame, "SOLUSDT")
    store(session, random_walk(200, seed=22), "ETHUSDT")
    bus = EventBus()
    scanner = MarketScanner(session, Settings(), bus)
    first = await scanner.scan("binance", "1h", ["SOLUSDT", "ETHUSDT", "NODATAUSDT"])
    statuses = {item["symbol"]: item["status"] for item in first["results"]}
    assert statuses["SOLUSDT"] == "OPPORTUNITY" and statuses["NODATAUSDT"] == "INSUFFICIENT_HISTORY"
    second = await scanner.scan("binance", "1h", ["SOLUSDT"])
    assert second["results"][0]["created"] is False
    opportunities = session.scalars(select(Opportunity).where(Opportunity.symbol == "SOLUSDT")).all()
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.source == "scanner" and opportunity.strategy_version_id is None
    assert "VOLUME_SPIKE" in opportunity.reasons and 0 < opportunity.rank_score <= 100
    assert "not a prediction" in opportunity.rank_breakdown["note"]
    assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "OPPORTUNITY_CREATED")) == 1
    from app.models import Order, TradeIntent
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    assert session.scalar(select(func.count()).select_from(TradeIntent)) == 0


@pytest.mark.asyncio
async def test_scanner_attaches_relevant_news_and_new_candle_refreshes_same_opportunity(session):
    from app.global_services.market_intelligence import IntelligenceService, RawIntelligenceItem

    frame = random_walk(201, seed=31)
    frame.loc[frame.index[-2:], "volume"] = frame["volume"].median() * 8
    store(session, frame.iloc[:-1], "SOLUSDT")
    await IntelligenceService(session, EventBus()).store([RawIntelligenceItem(
        provider="fixture", kind="news", external_id="n1", title="Solana network upgrade scheduled",
        published_at=frame["timestamp"].iloc[-2].to_pydatetime() - timedelta(hours=1),
        url="https://example.test/n1", currencies=["SOL"],
    )])
    scanner = MarketScanner(session, Settings(), EventBus())
    await scanner.scan("binance", "1h", ["SOLUSDT"])
    store(session, frame.iloc[-1:], "SOLUSDT")
    await scanner.scan("binance", "1h", ["SOLUSDT"])
    opportunity = session.scalar(select(Opportunity).where(Opportunity.source == "scanner"))
    assert opportunity.observations == 2 and len(opportunity.event_ids) == 1
