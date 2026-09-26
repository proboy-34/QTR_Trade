import asyncio
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

import httpx
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.decimal_math import decimal, rate
from app.core.time import TimeService
from app.global_services.market_data import MarketDataQuality
from app.models import MarketCandle, MarketDataBackfill, MarketDataValidationFailure

TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
TIMEFRAME_DELTA = {key: timedelta(milliseconds=value) for key, value in TIMEFRAME_MS.items()}


class HistoricalCandleProvider(Protocol):
    name: str

    async def fetch_batch(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int
    ) -> list[dict]: ...


class BinanceHistoricalProvider:
    name = "binance"

    def __init__(self, base_url: str = "https://api.binance.com") -> None:
        self.base_url = base_url

    async def fetch_batch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[dict]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15) as client:
            response = await client.get("/api/v3/klines", params={
                "symbol": symbol, "interval": timeframe,
                "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000),
                "limit": min(limit, 1000),
            })
            response.raise_for_status()
            return [self._normalize(item) for item in response.json()]

    @staticmethod
    def _normalize(item: list) -> dict:
        return {"timestamp": datetime.fromtimestamp(item[0] / 1000, UTC), "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5]}


class OKXHistoricalProvider:
    name = "okx"
    bars = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}

    async def fetch_batch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[dict]:
        instrument = symbol if "-" in symbol else symbol.replace("USDT", "-USDT")
        async with httpx.AsyncClient(base_url="https://www.okx.com", timeout=15) as client:
            response = await client.get("/api/v5/market/history-candles", params={
                "instId": instrument, "bar": self.bars[timeframe], "after": int(start.timestamp() * 1000),
                "before": int(end.timestamp() * 1000), "limit": min(limit, 300),
            })
            response.raise_for_status()
            rows = response.json()["data"]
            return sorted(({"timestamp": datetime.fromtimestamp(int(item[0]) / 1000, UTC), "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5]} for item in rows), key=lambda item: item["timestamp"])


class BybitHistoricalProvider:
    name = "bybit"
    intervals = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}

    async def fetch_batch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[dict]:
        async with httpx.AsyncClient(base_url="https://api.bybit.com", timeout=15) as client:
            response = await client.get("/v5/market/kline", params={
                "category": "spot", "symbol": symbol, "interval": self.intervals[timeframe],
                "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000),
                "limit": min(limit, 1000),
            })
            response.raise_for_status()
            rows = response.json()["result"]["list"]
            return sorted(({"timestamp": datetime.fromtimestamp(int(item[0]) / 1000, UTC), "open": item[1], "high": item[2], "low": item[3], "close": item[4], "volume": item[5]} for item in rows), key=lambda item: item["timestamp"])


class SyntheticHistoricalProvider:
    """Deterministic local provider for demo/backfill testing without network access."""

    name = "paper"

    async def fetch_batch(self, symbol: str, timeframe: str, start: datetime, end: datetime, limit: int) -> list[dict]:
        interval = TIMEFRAME_DELTA[timeframe]
        rows: list[dict] = []
        current = start
        while current <= end and len(rows) < limit:
            index = int(current.timestamp() // max(1, interval.total_seconds()))
            base = Decimal("50000") + Decimal(index % 10_000) / Decimal("10")
            wave = Decimal(str(round(math.sin(index / 17) * 25, 8)))
            open_price = base + wave
            close = open_price + Decimal(str((index % 7) - 3))
            rows.append({
                "timestamp": current, "open": open_price, "high": max(open_price, close) + 5,
                "low": min(open_price, close) - 5, "close": close,
                "volume": Decimal("1000") + Decimal(index % 200),
            })
            current += interval
        return rows


HISTORICAL_PROVIDERS: dict[str, HistoricalCandleProvider] = {
    "paper": SyntheticHistoricalProvider(),
    "binance": BinanceHistoricalProvider(get_settings().binance_public_base_url),
    "okx": OKXHistoricalProvider(),
    "bybit": BybitHistoricalProvider(),
}


class HistoricalBackfillService:
    def __init__(self, session: Session, max_retries: int = 3) -> None:
        self.session = session
        self.max_retries = max_retries

    def create(
        self, exchange: str, symbol: str, timeframe: str, start: datetime, end: datetime,
        batch_limit: int = 500,
    ) -> MarketDataBackfill:
        if timeframe not in TIMEFRAME_DELTA or end < start:
            raise ValueError("Invalid timeframe or date range")
        job = MarketDataBackfill(
            exchange=exchange, symbol=symbol, timeframe=timeframe, start_at=start, end_at=end,
            next_start_at=start, status="QUEUED", batch_limit=batch_limit,
        )
        self.session.add(job)
        self.session.commit()
        return job

    async def run(self, job: MarketDataBackfill, provider: HistoricalCandleProvider) -> MarketDataBackfill:
        # SQLite returns naive datetimes; a naive value's .timestamp() would use the host's local
        # timezone (e.g. on Windows), silently shifting provider request windows.
        job.start_at = TimeService.ensure_utc(job.start_at)
        job.end_at = TimeService.ensure_utc(job.end_at)
        job.next_start_at = TimeService.ensure_utc(job.next_start_at)
        job.status = "RUNNING"
        job.failure_reason = None
        self.session.commit()
        interval = TIMEFRAME_DELTA[job.timeframe]
        while job.next_start_at <= job.end_at:
            batch = await self._fetch_with_retries(job, provider)
            if batch is None:
                return job
            if not batch:
                job.status = "FAILED"
                job.failure_reason = "PROVIDER_EMPTY_BATCH"
                self.session.commit()
                return job
            frame = pd.DataFrame(batch)
            report = MarketDataQuality().validate(
                frame, job.timeframe, now=max(item["timestamp"] for item in batch),
                stale_after_seconds=max(1, int(interval.total_seconds() * 2)),
            )
            if not report.valid:
                self.session.add(MarketDataValidationFailure(
                    backfill_id=job.id, exchange=job.exchange, symbol=job.symbol,
                    timestamp=batch[0].get("timestamp"), error_codes=report.errors,
                    payload={key: str(value) for key, value in batch[0].items()},
                ))
                job.status = "FAILED"
                job.failure_reason = ",".join(report.errors)
                self.session.commit()
                return job
            timestamps = [item["timestamp"] for item in batch]
            existing = self.session.scalars(select(MarketCandle.timestamp).where(
                MarketCandle.exchange == job.exchange,
                MarketCandle.symbol == job.symbol,
                MarketCandle.timeframe == job.timeframe,
                MarketCandle.timestamp.in_(timestamps),
            )).all()
            # SQLite drops timezone metadata; compare canonical UTC wall times so
            # a resumed/imported range cannot bypass deduplication.
            def timestamp_key(value: datetime) -> datetime:
                if value.tzinfo:
                    return value.astimezone(UTC).replace(tzinfo=None)
                return value

            existing_keys = {timestamp_key(value) for value in existing}
            written = 0
            for item in batch:
                if timestamp_key(item["timestamp"]) in existing_keys:
                    continue
                self.session.add(MarketCandle(
                    exchange=job.exchange, symbol=job.symbol, timeframe=job.timeframe,
                    timestamp=item["timestamp"], open=decimal(item["open"]), high=decimal(item["high"]),
                    low=decimal(item["low"]), close=decimal(item["close"]),
                    volume=decimal(item["volume"]), is_demo=job.exchange == "paper",
                ))
                written += 1
            last = max(timestamps)
            job.rows_written += written
            job.last_success_at = last
            job.next_start_at = last + interval
            total = max(1, (job.end_at - job.start_at).total_seconds())
            completed = min(total, (last - job.start_at).total_seconds() + interval.total_seconds())
            job.progress = rate(completed / total)
            self.session.commit()  # checkpoint each bounded batch
        job.status = "COMPLETED"
        job.progress = Decimal("1")
        self.session.commit()
        return job

    async def _fetch_with_retries(
        self, job: MarketDataBackfill, provider: HistoricalCandleProvider
    ) -> list[dict] | None:
        for attempt in range(self.max_retries):
            try:
                return await provider.fetch_batch(
                    job.symbol, job.timeframe, job.next_start_at, job.end_at, job.batch_limit
                )
            except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
                job.retry_count += 1
                job.failure_reason = f"{type(exc).__name__}: {exc}"
                self.session.commit()
                if attempt + 1 < self.max_retries:
                    await asyncio.sleep(min(2**attempt, 2))
        job.status = "FAILED"
        self.session.commit()
        return None
