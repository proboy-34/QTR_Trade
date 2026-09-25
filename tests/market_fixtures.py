"""Deterministic synthetic market fixtures for tests only (never used by production code)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd

from app.global_services.universe import InstrumentInfo, MarketStats
from app.models import MarketCandle

EMA_SPEC = {
    "entry": [{"left": "ema:{fast}", "operator": "crosses_above", "right": "ema:{slow}"}],
    "exit": [{"left": "ema:{fast}", "operator": "crosses_below", "right": "ema:{slow}"}],
    "timeframes": ["1h"], "universe": ["SOLUSDT"], "parameters": {"fast": 5, "slow": 15},
    "risk": {"stop_loss_pct": 0.04, "take_profit_pct": 0.08},
}


def oscillating(n: int, seed: int = 3, drift: float = 0.0, amplitude: float = 0.05, period: int = 48,
                noise: float = 0.007, base: float = 100.0, end: datetime | None = None) -> pd.DataFrame:
    """A driftless oscillating market: a genuine, repeatable timing edge for trend-change rules."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base * np.exp(drift * t) * (1 + amplitude * np.sin(2 * np.pi * t / period)) * np.exp(np.cumsum(rng.normal(0, noise, n)))
    end = end or datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    frame = pd.DataFrame({
        "timestamp": pd.date_range(end=end, periods=n, freq="h", tz="UTC"),
        "open": np.r_[close[0], close[:-1]], "close": close,
    })
    frame["high"] = np.maximum(frame["open"], frame["close"]) * 1.001
    frame["low"] = np.minimum(frame["open"], frame["close"]) * 0.999
    frame["volume"] = 1000 + rng.random(n) * 200
    return frame


def random_walk(n: int, seed: int, end: datetime | None = None) -> pd.DataFrame:
    return oscillating(n, seed=seed, amplitude=0.0, end=end)


def store(session, frame: pd.DataFrame, symbol: str, exchange: str = "binance", timeframe: str = "1h") -> None:
    for row in frame.itertuples():
        session.add(MarketCandle(
            exchange=exchange, symbol=symbol, timeframe=timeframe, timestamp=row.timestamp.to_pydatetime(),
            open=Decimal(str(round(row.open, 8))), high=Decimal(str(round(row.high, 8))),
            low=Decimal(str(round(row.low, 8))), close=Decimal(str(round(row.close, 8))),
            volume=Decimal(str(round(row.volume, 6))), is_demo=False,
        ))
    session.commit()


def candle_payload(row, symbol: str) -> dict:
    timestamp = row.timestamp.to_pydatetime()
    return {
        "symbol": symbol, "timeframe": "1h", "timestamp": timestamp, "observed_at": timestamp + timedelta(hours=1, seconds=1),
        "open": str(round(row.open, 8)), "high": str(round(row.high, 8)), "low": str(round(row.low, 8)),
        "close": str(round(row.close, 8)), "volume": str(round(row.volume, 6)), "closed": True,
    }


def instrument(symbol: str, base: str, quote: str = "USDT", status: str = "TRADING", tick: str = "0.01",
               step: str = "0.001") -> InstrumentInfo:
    return InstrumentInfo(symbol, base, quote, status, Decimal(tick), Decimal(step), Decimal(step), Decimal("5"))


class FakeUniverseProvider:
    name = "binance"

    def __init__(self, instruments: list[InstrumentInfo], stats: dict[str, MarketStats]) -> None:
        self._instruments = instruments
        self._stats = stats

    async def instruments(self) -> list[InstrumentInfo]:
        return self._instruments

    async def market_stats(self) -> dict[str, MarketStats]:
        return self._stats


def liquid(symbol: str, price: float = 100.0, volume: float = 5e8, change: float = 1.0) -> MarketStats:
    return MarketStats(symbol, price, volume, change, 100_000, bid=price * 0.9999, ask=price * 1.0001)
