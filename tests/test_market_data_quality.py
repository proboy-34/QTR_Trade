from datetime import UTC, datetime, timedelta

import pandas as pd

from app.global_services.market_data import MarketDataQuality


def valid_frame() -> pd.DataFrame:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return pd.DataFrame({
        "timestamp": [now - timedelta(hours=2), now - timedelta(hours=1), now],
        "open": [100, 101, 102],
        "high": [102, 103, 104],
        "low": [99, 100, 101],
        "close": [101, 102, 103],
        "volume": [10, 11, 12],
    })


def test_valid_market_data_passes():
    assert MarketDataQuality().validate(valid_frame(), "1h").valid


def test_duplicate_candles_are_rejected():
    data = valid_frame()
    data.loc[2, "timestamp"] = data.loc[1, "timestamp"]
    assert "DUPLICATE_CANDLES" in MarketDataQuality().validate(data, "1h").errors


def test_out_of_order_candles_are_rejected():
    data = valid_frame().iloc[[0, 2, 1]].reset_index(drop=True)
    assert "OUT_OF_ORDER_TIMESTAMPS" in MarketDataQuality().validate(data, "1h").errors


def test_invalid_price_and_volume_are_rejected():
    data = valid_frame()
    data.loc[1, "close"] = -1
    data.loc[2, "volume"] = -1
    report = MarketDataQuality().validate(data, "1h")
    assert {"INVALID_PRICE", "INVALID_VOLUME"}.issubset(report.errors)


def test_missing_candle_is_reported():
    data = valid_frame().drop(index=1)
    report = MarketDataQuality().validate(data, "1h")
    assert report.missing_intervals == 1
    assert "MISSING_CANDLES" in report.warnings


def test_stale_feed_is_rejected():
    data = valid_frame()
    future = datetime.now(UTC) + timedelta(days=1)
    assert "STALE_FEED" in MarketDataQuality().validate(data, "1h", now=future).errors


def test_exchange_downtime_empty_feed_is_rejected():
    report = MarketDataQuality().validate(pd.DataFrame(), "1h")
    assert not report.valid and report.errors == ["EMPTY_FEED"]


def test_future_timestamp_is_rejected():
    data = valid_frame()
    data.loc[2, "timestamp"] = datetime.now(UTC) + timedelta(minutes=10)
    assert "FUTURE_TIMESTAMP" in MarketDataQuality().validate(data, "1h").errors
