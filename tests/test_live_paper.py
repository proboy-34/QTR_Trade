import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.global_services.live_paper import BinanceKlineStream, LivePaperService
from app.global_services.market_data import MarketDataQuality
from app.models import Dataset, Decision, LivePaperSession, MarketCandle


def hourly_history(session, rows: int = 35) -> datetime:
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    start = end - timedelta(hours=rows - 1)
    for index in range(rows):
        value = 60_000 + index * 10
        session.add(MarketCandle(
            exchange="binance", symbol="BTCUSDT", timeframe="1h",
            timestamp=start + timedelta(hours=index), open=value, high=value + 20,
            low=value - 20, close=value + 10, volume=1000 + index, is_demo=False,
        ))
    session.commit()
    return end


def service_for(session, **settings) -> LivePaperService:
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    return LivePaperService(factory, Settings(**settings), EventBus())


def test_binance_kline_normalization():
    event = BinanceKlineStream.normalize({
        "E": 1_735_689_601_000,
        "k": {
            "s": "BTCUSDT", "i": "1m", "t": 1_735_689_540_000,
            "o": "100.1", "h": "102.0", "l": "99.5", "c": "101.2",
            "v": "12.34", "x": True,
        },
    })
    assert event["symbol"] == "BTCUSDT" and event["closed"] is True
    assert event["close"] == "101.2" and event["timestamp"].tzinfo is UTC


def test_market_quality_accepts_exchange_numeric_strings():
    timestamp = datetime.now(UTC).replace(second=0, microsecond=0)
    frame = pd.DataFrame([{
        "timestamp": timestamp, "open": "100.1", "high": "101.2",
        "low": "99.9", "close": "100.8", "volume": "12.3",
    }])
    assert MarketDataQuality().validate(frame, "1m", now=timestamp).valid


@pytest.mark.asyncio
async def test_closed_live_candle_is_persisted_decided_and_duplicate_safe(session):
    end = hourly_history(session)
    service = service_for(session)
    service.state.status = "LIVE"
    service.state.connected = True
    service.state.last_message_at = end + timedelta(hours=1, seconds=1)
    candle = {
        "symbol": "BTCUSDT", "timeframe": "1h",
        "timestamp": end + timedelta(hours=1),
        "observed_at": end + timedelta(hours=2),
        "open": "60350", "high": "60380", "low": "60340",
        "close": "60370", "volume": "1500", "closed": True,
    }
    assert await service.ingest_closed_candle(candle)
    assert not await service.ingest_closed_candle(candle)
    assert session.scalar(select(func.count()).select_from(MarketCandle).where(
        MarketCandle.exchange == "binance"
    )) == 36
    assert session.scalar(select(func.count()).select_from(Decision)) == 1
    dataset = session.scalar(select(Dataset).where(
        Dataset.name == "binance:BTCUSDT:1h:live"
    ))
    assert dataset is not None and dataset.row_count == 36 and not dataset.is_demo
    assert service.state.candles_received == 1 and service.state.decisions_run == 1


@pytest.mark.asyncio
async def test_live_service_start_stop_with_fake_stream(session):
    end = hourly_history(session)
    release = asyncio.Event()

    class FakeStream:
        async def messages(self, symbol: str, timeframe: str):
            yield {
                "symbol": symbol, "timeframe": timeframe, "timestamp": end,
                "observed_at": datetime.now(UTC), "open": "60000", "high": "60100",
                "low": "59900", "close": "60050", "volume": "100", "closed": False,
            }
            await release.wait()

    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    service = LivePaperService(factory, Settings(), EventBus(), FakeStream())
    started = await service.start("binance", "BTCUSDT", "1h")
    assert started["paper_only"] and not started["real_orders_enabled"]
    for _ in range(30):
        if service.state.connected:
            break
        await asyncio.sleep(.01)
    assert service.state.connected and service.state.latest_price == "60050"
    stopped = await service.stop()
    assert stopped["status"] == "STOPPED" and not stopped["running"]
    record = session.get(LivePaperSession, service.state.session_id)
    assert record is not None and record.status == "STOPPED"


def test_interrupted_live_session_is_recovered(session):
    session.add(LivePaperSession(
        provider="binance", symbol="BTCUSDT", timeframe="1h", status="LIVE",
        configuration={"paper_only": True},
    ))
    session.commit()
    service = service_for(session)
    assert service.recover_interrupted() == 1
    record = session.scalar(select(LivePaperSession))
    session.refresh(record)
    assert record.status == "INTERRUPTED" and record.stopped_at is not None


@pytest.mark.asyncio
async def test_live_data_service_refuses_non_paper_mode(session):
    service = service_for(
        session, trading_mode="live", live_trading_enabled=True,
        live_trading_confirmation="ENABLE_REAL_ORDERS",
    )
    with pytest.raises(SafetyError, match="requires TRADING_MODE=paper"):
        await service.start("binance", "BTCUSDT", "1h")
