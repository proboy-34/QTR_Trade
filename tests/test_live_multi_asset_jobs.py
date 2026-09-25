import asyncio
from datetime import UTC, datetime

import pytest
from market_fixtures import oscillating, store
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.events import EventBus
from app.global_services.live_paper import BinanceKlineStream, LivePaperService
from app.jobs import JobContext, jobs, run_job
from app.models import JobExecution, MarketCandle, SafetyControl, SystemEvent
from app.trading.safety import SafetyService


def test_combined_stream_payload_normalization():
    wrapped = {"stream": "ethusdt@kline_1h", "data": {"E": 1_758_000_000_000, "k": {
        "s": "ETHUSDT", "i": "1h", "t": 1_757_995_200_000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "x": False}}}
    event = BinanceKlineStream.normalize(wrapped["data"])
    assert event["symbol"] == "ETHUSDT" and event["closed"] is False
    assert BinanceKlineStream("wss://example.test:9443/").root == "wss://example.test:9443"


@pytest.mark.asyncio
async def test_multi_asset_live_paper_routes_prices_and_candles_per_symbol_and_reconnects(session):
    frames = {"SOLUSDT": oscillating(80, seed=1), "ETHUSDT": oscillating(80, seed=2, base=2500)}
    for symbol, frame in frames.items():
        store(session, frame.iloc[:-1], symbol)
    release = asyncio.Event()
    attempts = {"count": 0}

    class FlakyMultiStream:
        async def messages(self, symbol, timeframe):  # pragma: no cover - multi path is used
            raise AssertionError("single-symbol stream must not be used for multiple symbols")
            yield {}

        async def messages_multi(self, symbols, timeframe):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConnectionError("dropped")
            for symbol in symbols:
                row = frames[symbol].iloc[-1]
                yield {"symbol": symbol, "timeframe": timeframe, "timestamp": row.timestamp.to_pydatetime(),
                       "observed_at": datetime.now(UTC), "open": str(row.open), "high": str(row.high),
                       "low": str(row.low), "close": str(row.close), "volume": str(row.volume), "closed": True}
            await release.wait()

    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    service = LivePaperService(factory, Settings(live_paper_max_backoff_seconds=1), EventBus(), FlakyMultiStream())
    await service.start("binance", "SOLUSDT", "1h", ["solusdt", "ETHUSDT"])
    for _ in range(300):
        if service.state.candles_received >= 2:
            break
        await asyncio.sleep(0.01)
    assert service.state.reconnects == 1 and service.state.symbols == ["SOLUSDT", "ETHUSDT"]
    assert set(service.state.prices) == {"SOLUSDT", "ETHUSDT"} and service.state.candles_received == 2
    assert service.snapshot()["real_orders_enabled"] is False
    await service.stop()
    for symbol in frames:
        assert session.scalar(select(func.count()).select_from(MarketCandle).where(MarketCandle.symbol == symbol)) == 80
    types = set(session.scalars(select(SystemEvent.type)).all())
    assert {"ExchangeDisconnected", "ExchangeReconnected", "CANDLE_CLOSED"} <= types


@pytest.mark.asyncio
async def test_job_runner_persists_outcomes_and_respects_system_stop(session):
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    context = JobContext(factory, Settings(), EventBus())

    async def ok(_session):
        return {"value": 1, "when": datetime.now(UTC)}

    async def broken(_session):
        raise RuntimeError("provider down")

    assert (await run_job(context, "ok_job", ok))["value"] == 1
    with pytest.raises(RuntimeError, match="provider down"):
        await run_job(context, "broken_job", broken)
    records = {row.job_name: row for row in session.scalars(select(JobExecution)).all()}
    assert records["ok_job"].status == "COMPLETED" and isinstance(records["ok_job"].details["when"], str)
    assert records["broken_job"].status == "FAILED" and "provider down" in records["broken_job"].error
    registered = jobs(context)
    assert {"market_scan", "research_queue", "learning", "safety_monitor", "news_ingestion", "universe_refresh"} <= set(registered)
    news = await run_job(context, "news_ingestion", registered["news_ingestion"][1])
    assert "no news/macro provider configured" in news["skipped"]
    SafetyService(session).activate("SYSTEM", "maintenance")
    session.commit()
    assert (await run_job(context, "market_scan", registered["market_scan"][1]))["skipped"] == "SYSTEM_STOP"
    assert (await run_job(context, "research_queue", registered["research_queue"][1]))["skipped"] == "SYSTEM_STOP"
    assert session.scalar(select(func.count()).select_from(SafetyControl)) == 1
    disabled = jobs(JobContext(factory, Settings(market_scanner_enabled=False, market_sync_enabled=False), EventBus()))
    assert "market_scan" not in disabled and "market_data_sync" not in disabled
