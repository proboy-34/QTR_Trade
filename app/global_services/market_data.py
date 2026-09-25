from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pandas as pd


class HistoricalDataSource(Protocol):
    async def candles(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[dict]: ...


@dataclass
class DataQualityReport:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_intervals: int = 0


class MarketDataQuality:
    timeframe_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}

    def validate(
        self,
        frame: pd.DataFrame,
        timeframe: str,
        *,
        now: datetime | None = None,
        stale_after_seconds: int = 7200,
    ) -> DataQualityReport:
        errors: list[str] = []
        warnings: list[str] = []
        if frame.empty:
            return DataQualityReport(False, ["EMPTY_FEED"])
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(frame.columns)
        if missing:
            return DataQualityReport(False, [f"MISSING_FIELDS:{','.join(sorted(missing))}"])
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        if timestamps.duplicated().any():
            errors.append("DUPLICATE_CANDLES")
        if not timestamps.is_monotonic_increasing:
            errors.append("OUT_OF_ORDER_TIMESTAMPS")
        price_columns = frame[["open", "high", "low", "close"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if price_columns.isna().any().any() or (price_columns <= 0).any().any():
            errors.append("INVALID_PRICE")
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        if volume.isna().any() or (volume < 0).any():
            errors.append("INVALID_VOLUME")
        if (price_columns["high"] < price_columns[["open", "close", "low"]].max(axis=1)).any() or (
            price_columns["low"] > price_columns[["open", "close", "high"]].min(axis=1)
        ).any():
            errors.append("MALFORMED_OHLC")
        interval = self.timeframe_minutes.get(timeframe)
        missing_intervals = 0
        if interval and len(timestamps) > 1:
            expected = timedelta(minutes=interval)
            gaps = timestamps.diff().dropna()
            missing_intervals = int(sum(max(0, round(gap / expected) - 1) for gap in gaps))
            if missing_intervals:
                warnings.append("MISSING_CANDLES")
        current = now or datetime.now(UTC)
        latest = timestamps.iloc[-1].to_pydatetime()
        if latest > current + timedelta(seconds=5):
            errors.append("FUTURE_TIMESTAMP")
        if current - latest > timedelta(seconds=stale_after_seconds):
            errors.append("STALE_FEED")
        if interval and any(
            stamp.second != 0 or stamp.microsecond != 0 or stamp.minute % interval != 0
            for stamp in timestamps.array.to_pydatetime()
            if interval <= 60
        ):
            errors.append("EXCHANGE_TIMESTAMP_MISALIGNED")
        return DataQualityReport(not errors, errors, warnings, missing_intervals)

    def validate_snapshot(self, price: float, volume: float, observed_at: datetime) -> None:
        if price <= 0:
            raise ValueError("INVALID_PRICE")
        if volume < 0:
            raise ValueError("INVALID_VOLUME")
        timestamp = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC)
        if timestamp > datetime.now(UTC) + timedelta(seconds=5):
            raise ValueError("FUTURE_TIMESTAMP")
