"""Market memory: persisted market states and evidence-based historical similarity search."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.decimal_math import decimal
from app.core.time import TimeService
from app.global_services.features import rsi
from app.global_services.historical import TIMEFRAME_DELTA
from app.global_services.regime import regime_features
from app.models import MarketCandle, MarketContextRecord

FEATURE_KEYS = (
    "return_1", "return_10", "return_24", "volatility", "vol_pct", "volume_ratio",
    "rsi", "atr_pct", "efficiency", "ema_spread", "drawdown_50",
)


def context_features(frame: pd.DataFrame) -> dict[str, float | None]:
    """Numeric description of the latest bar using only past and current data."""
    if len(frame) < 25:
        return {}
    data = regime_features(frame.sort_values("timestamp").reset_index(drop=True))
    close = data["close"]
    true_range = pd.concat([
        data["high"] - data["low"], (data["high"] - close.shift(1)).abs(), (data["low"] - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    values = {
        "return_1": close.pct_change(1).iloc[-1],
        "return_10": close.pct_change(10).iloc[-1],
        "return_24": close.pct_change(24).iloc[-1] if len(close) > 24 else np.nan,
        "volatility": data["vol"].iloc[-1],
        "vol_pct": data["vol_pct"].iloc[-1],
        "volume_ratio": data["volume_ratio"].iloc[-1],
        "rsi": rsi(close).iloc[-1],
        "atr_pct": (atr / close).iloc[-1],
        "efficiency": data["efficiency"].iloc[-1],
        "ema_spread": data["ema_spread"].iloc[-1],
        "drawdown_50": (close / data["high"].rolling(50, min_periods=10).max() - 1).iloc[-1],
    }
    return {key: (round(float(value), 6) if pd.notna(value) and np.isfinite(value) else None) for key, value in values.items()}


@dataclass
class SimilarSituation:
    context_id: str
    symbol: str
    timeframe: str
    observed_at: str
    regime: str | None
    distance: float
    forward_return_pct: float | None


class MarketMemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def remember(
        self, *, exchange: str, symbol: str, timeframe: str, observed_at: datetime, price: Any,
        features: dict[str, Any], regime: str, regime_confidence: float, legacy_regime: str,
        direction: str, extra: dict[str, Any] | None = None,
    ) -> MarketContextRecord:
        """Idempotent per closed candle: one context row per exchange/symbol/timeframe/bar."""
        observed_at = TimeService.ensure_utc(observed_at)
        existing = self.session.scalar(select(MarketContextRecord).where(
            MarketContextRecord.exchange == exchange, MarketContextRecord.symbol == symbol,
            MarketContextRecord.timeframe == timeframe, MarketContextRecord.observed_at == observed_at,
        ))
        if existing:
            return existing
        previous = self.session.scalar(select(MarketContextRecord).where(
            MarketContextRecord.symbol == symbol, MarketContextRecord.timeframe == timeframe,
        ).order_by(MarketContextRecord.observed_at.desc()))
        record = MarketContextRecord(
            symbol=symbol, exchange=exchange, timeframe=timeframe, price=decimal(price),
            regime=legacy_regime, direction=direction,
            volatility=float(features.get("volatility") or 0), liquidity="UNKNOWN",
            previous_regime=previous.market_regime if previous else None,
            market_regime=regime, regime_confidence=regime_confidence, features=features,
            snapshot={"source": "market_memory", **(extra or {})}, observed_at=observed_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def similar(
        self, features: dict[str, Any], *, timeframe: str, symbol: str | None = None,
        limit: int = 10, horizon_bars: int = 12, exclude_after: datetime | None = None,
        candidates: int = 2000,
    ) -> dict[str, Any]:
        """Nearest historical contexts by standardized distance. Returns evidence, not a forecast."""
        keys = [key for key in FEATURE_KEYS if features.get(key) is not None]
        if not keys:
            return {"matches": [], "summary": {"count": 0}, "note": "No comparable features supplied"}
        query = select(MarketContextRecord).where(MarketContextRecord.timeframe == timeframe)
        if symbol:
            query = query.where(MarketContextRecord.symbol == symbol)
        if exclude_after:
            query = query.where(MarketContextRecord.observed_at < exclude_after)
        rows = [
            row for row in self.session.scalars(
                query.order_by(MarketContextRecord.observed_at.desc()).limit(candidates)
            ).all()
            if row.features and all(row.features.get(key) is not None for key in keys)
        ]
        if not rows:
            return {"matches": [], "summary": {"count": 0}, "note": "No historical contexts with these features"}
        matrix = np.array([[float(row.features[key]) for key in keys] for row in rows])
        target = np.array([float(features[key]) for key in keys])
        scale = matrix.std(axis=0)
        scale[scale == 0] = 1
        distances = np.sqrt((((matrix - target) / scale) ** 2).sum(axis=1))
        order = np.argsort(distances)[:limit]
        matches: list[SimilarSituation] = []
        for index in order:
            row = rows[int(index)]
            matches.append(SimilarSituation(
                row.id, row.symbol, row.timeframe or timeframe, row.observed_at.isoformat(),
                row.market_regime, round(float(distances[index]), 4),
                self.forward_return(row, horizon_bars),
            ))
        outcomes = [item.forward_return_pct for item in matches if item.forward_return_pct is not None]
        return {
            "features_used": keys,
            "matches": [item.__dict__ for item in matches],
            "summary": {
                "count": len(matches),
                "with_known_outcome": len(outcomes),
                "median_forward_return_pct": round(float(np.median(outcomes)), 4) if outcomes else None,
                "positive_share": round(sum(value > 0 for value in outcomes) / len(outcomes), 3) if outcomes else None,
                "horizon_bars": horizon_bars,
            },
            "note": "Historical analogues are evidence for research, not a prediction.",
        }

    def forward_return(self, context: MarketContextRecord, horizon_bars: int) -> float | None:
        if not context.timeframe or context.timeframe not in TIMEFRAME_DELTA or not context.exchange:
            return None
        target_at = TimeService.ensure_utc(context.observed_at) + TIMEFRAME_DELTA[context.timeframe] * horizon_bars
        future = self.session.scalar(select(MarketCandle).where(
            MarketCandle.exchange == context.exchange, MarketCandle.symbol == context.symbol,
            MarketCandle.timeframe == context.timeframe, MarketCandle.timestamp >= target_at,
            MarketCandle.timestamp < target_at + timedelta(seconds=TIMEFRAME_DELTA[context.timeframe].total_seconds()),
        ))
        if not future or not context.price:
            return None
        return round((float(future.close) / float(context.price) - 1) * 100, 4)
