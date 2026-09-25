import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import pandas as pd
from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from app.core.config import Settings
from app.core.decimal_math import decimal
from app.core.errors import SafetyError
from app.core.events import Event, EventBus, publish_persisted
from app.core.time import TimeService
from app.global_services.features import enrich
from app.global_services.historical import (
    HISTORICAL_PROVIDERS,
    TIMEFRAME_DELTA,
    TIMEFRAME_MS,
    HistoricalBackfillService,
)
from app.global_services.market_data import MarketDataQuality
from app.models import (
    Dataset,
    LivePaperSession,
    MarketCandle,
    MarketDataValidationFailure,
)
from app.trading.loop import PaperTradingLoop
from app.trading.pipeline import MarketSnapshot


class LiveCandleStream(Protocol):
    def messages(self, symbol: str, timeframe: str) -> AsyncIterator[dict[str, Any]]: ...


class BinanceKlineStream:
    """Public Binance Spot kline stream. It never authenticates or submits orders."""

    base_url = "wss://stream.binance.com:9443/ws"

    async def messages(self, symbol: str, timeframe: str) -> AsyncIterator[dict[str, Any]]:
        stream = f"{symbol.lower()}@kline_{timeframe}"
        async with connect(
            f"{self.base_url}/{stream}", ping_interval=20, ping_timeout=20,
            close_timeout=10, max_queue=1000,
        ) as websocket:
            async for raw in websocket:
                payload = json.loads(raw)
                yield self.normalize(payload)

    @staticmethod
    def normalize(payload: dict[str, Any]) -> dict[str, Any]:
        candle = payload["k"]
        return {
            "symbol": candle["s"],
            "timeframe": candle["i"],
            "timestamp": datetime.fromtimestamp(int(candle["t"]) / 1000, UTC),
            "observed_at": datetime.fromtimestamp(int(payload["E"]) / 1000, UTC),
            "open": candle["o"],
            "high": candle["h"],
            "low": candle["l"],
            "close": candle["c"],
            "volume": candle["v"],
            "closed": bool(candle["x"]),
        }


@dataclass
class LivePaperState:
    status: str = "STOPPED"
    provider: str = "binance"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    connected: bool = False
    started_at: datetime | None = None
    last_message_at: datetime | None = None
    last_candle_at: datetime | None = None
    latest_price: str | None = None
    candles_received: int = 0
    decisions_run: int = 0
    reconnects: int = 0
    last_decision: str | None = None
    last_error: str | None = None
    session_id: str | None = None


class LivePaperService:
    """Connects real public data to the existing paper-only trading pipeline."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        event_bus: EventBus,
        stream: LiveCandleStream | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.event_bus = event_bus
        self.stream = stream or BinanceKlineStream()
        self.state = LivePaperState(
            provider=settings.live_paper_provider,
            symbol=settings.live_paper_symbol,
            timeframe=settings.live_paper_timeframe,
        )
        self._task: asyncio.Task[None] | None = None
        self._desired = False
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self.state)
        for key in ("started_at", "last_message_at", "last_candle_at"):
            value = result[key]
            result[key] = value.isoformat() if value else None
        result.update({
            "paper_only": True,
            "real_orders_enabled": False,
            "trading_mode": self.settings.trading_mode,
            "running": bool(self._task and not self._task.done()),
        })
        return result

    def recover_interrupted(self) -> int:
        with self.session_factory() as session:
            identifiers = session.scalars(select(LivePaperSession.id).where(
                LivePaperSession.status.in_(["STARTING", "CONNECTING", "LIVE", "WARMING_UP"])
            )).all()
            session.execute(
                update(LivePaperSession)
                .where(LivePaperSession.status.in_(["STARTING", "CONNECTING", "LIVE", "WARMING_UP"]))
                .values(status="INTERRUPTED", stopped_at=TimeService.now())
            )
            session.commit()
            return len(identifiers)

    async def start(self, provider: str, symbol: str, timeframe: str) -> dict[str, Any]:
        async with self._lock:
            if self.settings.trading_mode != "paper":
                raise SafetyError("Live-data paper service requires TRADING_MODE=paper")
            if provider != "binance":
                raise SafetyError("Only the Binance public live-data adapter is enabled")
            if timeframe not in TIMEFRAME_DELTA:
                raise SafetyError("Unsupported live-paper timeframe")
            if self._task and not self._task.done():
                if (provider, symbol, timeframe) == (
                    self.state.provider, self.state.symbol, self.state.timeframe,
                ):
                    return self.snapshot()
                raise SafetyError("Stop the running live-paper session before changing its market")
            now = TimeService.now()
            with self.session_factory() as session:
                record = LivePaperSession(
                    provider=provider, symbol=symbol, timeframe=timeframe, status="STARTING",
                    started_at=now,
                    configuration={
                        "paper_only": True,
                        "bootstrap_candles": self.settings.live_paper_bootstrap_candles,
                    },
                )
                session.add(record)
                session.commit()
                session.refresh(record)
                session_id = record.id
            self.state = LivePaperState(
                status="STARTING", provider=provider, symbol=symbol, timeframe=timeframe,
                started_at=now, session_id=session_id,
            )
            self._desired = True
            self._task = asyncio.create_task(self._supervise(), name="qtr-live-paper")
            return self.snapshot()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            if not self._task and self.state.status == "STOPPED":
                return self.snapshot()
            self._desired = False
            task = self._task
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
            self.state.connected = False
            self.state.status = "STOPPED"
            self._update_record(status="STOPPED", stopped_at=TimeService.now())
            await self._publish("LivePaperStopped", {"session_id": self.state.session_id})
            return self.snapshot()

    async def _supervise(self) -> None:
        try:
            await self._bootstrap()
            while self._desired:
                try:
                    self.state.status = "CONNECTING"
                    self.state.connected = False
                    self._update_record(status="CONNECTING")
                    async for candle in self.stream.messages(self.state.symbol, self.state.timeframe):
                        if not self._desired:
                            break
                        first_message = not self.state.connected
                        self.state.connected = True
                        self.state.status = "LIVE"
                        self.state.last_error = None
                        self.state.last_message_at = candle.get("observed_at") or TimeService.now()
                        self.state.latest_price = str(candle["close"])
                        if first_message:
                            event_type = "ExchangeReconnected" if self.state.reconnects else "LivePaperConnected"
                            await self._publish(event_type, {
                                "provider": self.state.provider,
                                "symbol": self.state.symbol,
                                "timeframe": self.state.timeframe,
                            })
                        await self._monitor_price(float(candle["close"]))
                        if candle.get("closed"):
                            await self.ingest_closed_candle(candle)
                    if self._desired:
                        raise ConnectionError("Binance stream ended")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.state.connected = False
                    self.state.status = "RECONNECTING"
                    self.state.reconnects += 1
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    self._update_record(
                        status="RECONNECTING", reconnects=self.state.reconnects,
                        last_error=self.state.last_error,
                    )
                    await self._publish("ExchangeDisconnected", {
                        "provider": self.state.provider, "error": self.state.last_error,
                    })
                    delay = min(
                        2 ** min(self.state.reconnects - 1, 8),
                        self.settings.live_paper_max_backoff_seconds,
                    )
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.state.connected = False
            self.state.status = "ERROR"
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            self._update_record(status="ERROR", last_error=self.state.last_error)

    async def _bootstrap(self) -> None:
        with self.session_factory() as session:
            count = session.scalar(select(func.count()).select_from(MarketCandle).where(
                MarketCandle.exchange == self.state.provider,
                MarketCandle.symbol == self.state.symbol,
                MarketCandle.timeframe == self.state.timeframe,
            )) or 0
        if count >= 35:
            return
        self.state.status = "WARMING_UP"
        self._update_record(status="WARMING_UP")
        with self.session_factory() as session:
            interval_ms = TIMEFRAME_MS[self.state.timeframe]
            now_ms = int(TimeService.now().timestamp() * 1000)
            end_ms = (now_ms // interval_ms) * interval_ms - interval_ms
            start_ms = end_ms - interval_ms * (self.settings.live_paper_bootstrap_candles - 1)
            start = datetime.fromtimestamp(start_ms / 1000, UTC)
            end = datetime.fromtimestamp(end_ms / 1000, UTC)
            job = HistoricalBackfillService(session).create(
                self.state.provider, self.state.symbol, self.state.timeframe,
                start, end, min(500, self.settings.live_paper_bootstrap_candles),
            )
            provider = HISTORICAL_PROVIDERS[self.state.provider]
            await HistoricalBackfillService(session).run(job, provider)
            if job.status != "COMPLETED":
                raise ConnectionError(f"Historical warm-up failed: {job.failure_reason}")

    async def ingest_closed_candle(self, candle: dict[str, Any]) -> bool:
        timestamp = candle["timestamp"]
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        observed_at = candle.get("observed_at") or TimeService.now()
        frame = pd.DataFrame([{**candle, "timestamp": timestamp}])
        report = MarketDataQuality().validate(
            frame, self.state.timeframe, now=observed_at,
            stale_after_seconds=max(
                self.settings.market_data_stale_seconds,
                int(TIMEFRAME_DELTA[self.state.timeframe].total_seconds() * 3),
            ),
        )
        with self.session_factory() as session:
            if not report.valid:
                session.add(MarketDataValidationFailure(
                    exchange=self.state.provider, symbol=self.state.symbol,
                    timestamp=timestamp, error_codes=report.errors,
                    payload={key: str(value) for key, value in candle.items()},
                ))
                session.commit()
                self.state.last_error = ",".join(report.errors)
                return False
            existing = session.scalar(select(MarketCandle.id).where(
                MarketCandle.exchange == self.state.provider,
                MarketCandle.symbol == self.state.symbol,
                MarketCandle.timeframe == self.state.timeframe,
                MarketCandle.timestamp == timestamp,
            ))
            if existing:
                return False
            session.add(MarketCandle(
                exchange=self.state.provider, symbol=self.state.symbol,
                timeframe=self.state.timeframe, timestamp=timestamp,
                open=decimal(candle["open"]), high=decimal(candle["high"]),
                low=decimal(candle["low"]), close=decimal(candle["close"]),
                volume=decimal(candle["volume"]), is_demo=False,
            ))
            session.commit()
            self.state.candles_received += 1
            self.state.last_candle_at = timestamp
            result = await self._evaluate_latest(session, observed_at)
            if result:
                self.state.decisions_run += 1
                self.state.last_decision = result.get("outcome")
            self._update_dataset(session, timestamp)
            session.commit()
        self._update_record(
            status="LIVE", last_message_at=self.state.last_message_at,
            last_candle_at=timestamp, candles_received=self.state.candles_received,
            decisions_run=self.state.decisions_run, last_error=None,
        )
        return True

    async def _evaluate_latest(self, session: Session, observed_at: datetime) -> dict | None:
        rows = session.scalars(select(MarketCandle).where(
            MarketCandle.exchange == self.state.provider,
            MarketCandle.symbol == self.state.symbol,
            MarketCandle.timeframe == self.state.timeframe,
        ).order_by(desc(MarketCandle.timestamp)).limit(250)).all()
        if len(rows) < 35:
            self.state.status = "WARMING_UP"
            return None
        frame = pd.DataFrame([{
            "timestamp": item.timestamp, "open": item.open, "high": item.high,
            "low": item.low, "close": item.close, "volume": item.volume,
        } for item in reversed(rows)])
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        enriched = enrich(frame)
        latest = enriched.iloc[-1]
        volatility = float(latest["volatility"])
        if not math.isfinite(volatility):
            volatility = 0.0
        snapshot = MarketSnapshot(
            symbol=self.state.symbol, price=float(latest["close"]),
            volume=float(latest["volume"]), volatility=volatility, funding=0,
            regime=str(latest["regime"]).upper(), direction=str(latest["trend"]).upper(),
            observed_at=observed_at, exchange=self.state.provider,
            timeframe=self.state.timeframe,
        )
        return await PaperTradingLoop(session, self.settings, self.event_bus).process(snapshot)

    async def _monitor_price(self, price_value: float) -> None:
        with self.session_factory() as session:
            await PaperTradingLoop(session, self.settings, self.event_bus).monitor_price(
                self.state.symbol, price_value
            )

    def _update_dataset(self, session: Session, timestamp: datetime) -> None:
        name = f"{self.state.provider}:{self.state.symbol}:{self.state.timeframe}:live"
        dataset = session.scalar(select(Dataset).where(Dataset.name == name))
        count = session.scalar(select(func.count()).select_from(MarketCandle).where(
            MarketCandle.exchange == self.state.provider,
            MarketCandle.symbol == self.state.symbol,
            MarketCandle.timeframe == self.state.timeframe,
        )) or 0
        if dataset:
            dataset.row_count = count
            dataset.freshness_at = timestamp
        else:
            session.add(Dataset(
                name=name, symbol=self.state.symbol, timeframe=self.state.timeframe,
                source=f"{self.state.provider}-live", row_count=count,
                freshness_at=timestamp, is_demo=False,
            ))

    def _update_record(self, **values: Any) -> None:
        if not self.state.session_id:
            return
        with self.session_factory() as session:
            session.execute(update(LivePaperSession).where(
                LivePaperSession.id == self.state.session_id
            ).values(**values))
            session.commit()

    async def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        with self.session_factory() as session:
            await publish_persisted(
                session, self.event_bus,
                Event(event_type, payload, source="live_paper_service"),
                "live_paper",
            )
            session.commit()
