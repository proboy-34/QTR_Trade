"""Deterministic market regime classification.

The classifier scores every candidate regime from observable features and reports
``UNKNOWN`` when evidence is weak or contradictory instead of forcing a label.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.time import TimeService
from app.models import MarketCandle, MarketRegimeRecord

REGIMES = (
    "TRENDING_UP", "TRENDING_DOWN", "RANGING", "BREAKOUT", "REVERSAL",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "PANIC", "EUPHORIA", "UNKNOWN",
)
# Higher priority wins exact ties; extreme states dominate descriptive ones.
PRIORITY = {name: index for index, name in enumerate((
    "PANIC", "EUPHORIA", "BREAKOUT", "REVERSAL", "TRENDING_UP", "TRENDING_DOWN",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "RANGING",
))}
CONTRADICTORY = {
    frozenset({"TRENDING_UP", "TRENDING_DOWN"}), frozenset({"PANIC", "EUPHORIA"}),
    frozenset({"HIGH_VOLATILITY", "LOW_VOLATILITY"}), frozenset({"TRENDING_UP", "PANIC"}),
    frozenset({"TRENDING_DOWN", "EUPHORIA"}), frozenset({"RANGING", "BREAKOUT"}),
}
MIN_BARS = 60


@dataclass
class RegimeResult:
    regime: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


def _clip(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(min(1.0, max(0.0, value)))


def regime_features(frame: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    data = frame.copy()
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    close = data["close"]
    returns = close.pct_change()
    path = close.diff().abs().rolling(lookback).sum()
    data["efficiency"] = (close - close.shift(lookback)).abs() / path.replace(0, np.nan)
    data["trend_return"] = close / close.shift(lookback) - 1
    ema_fast = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema_slow = close.ewm(span=50, adjust=False, min_periods=50).mean()
    data["ema_spread"] = (ema_fast - ema_slow) / close
    data["vol"] = returns.rolling(lookback).std()
    data["vol_pct"] = data["vol"].rolling(100, min_periods=40).rank(pct=True)
    data["range_high"] = data["high"].shift(1).rolling(lookback).max()
    data["range_low"] = data["low"].shift(1).rolling(lookback).min()
    data["volume_ratio"] = data["volume"] / data["volume"].shift(1).rolling(lookback).median()
    data["short_return"] = close / close.shift(5) - 1
    # Shock relative to the recent drift: a steady trend is not a panic, an acceleration is.
    drift = returns.rolling(50, min_periods=lookback).mean()
    data["short_z"] = (data["short_return"] - 5 * drift) / (data["vol"] * np.sqrt(5)).replace(0, np.nan)
    prior = close.shift(5)
    prior_path = prior.diff().abs().rolling(lookback).sum()
    data["prior_efficiency"] = (prior - prior.shift(lookback)).abs() / prior_path.replace(0, np.nan)
    data["prior_return"] = prior / prior.shift(lookback) - 1
    return data


def score_row(row: pd.Series) -> dict[str, float]:
    efficiency = float(row.get("efficiency", np.nan))
    spread = float(row.get("ema_spread", np.nan))
    trend_return = float(row.get("trend_return", np.nan))
    vol_pct = float(row.get("vol_pct", np.nan))
    short_z = float(row.get("short_z", np.nan))
    volume_ratio = float(row.get("volume_ratio", np.nan))
    close = float(row["close"])
    # A trend is an orderly move; a violent shock is classified as panic/euphoria instead.
    orderly = 1 - _clip((abs(short_z) - 2) / 2) if np.isfinite(short_z) else 1.0
    trend_strength = _clip((efficiency - 0.2) / 0.4) * orderly
    scores = {
        "TRENDING_UP": trend_strength if spread > 0 and trend_return > 0 else 0.0,
        "TRENDING_DOWN": trend_strength if spread < 0 and trend_return < 0 else 0.0,
        "RANGING": _clip((0.3 - efficiency) / 0.2) * (1 - _clip((vol_pct - 0.8) / 0.2)),
        "HIGH_VOLATILITY": _clip((vol_pct - 0.8) / 0.15),
        "LOW_VOLATILITY": _clip((0.2 - vol_pct) / 0.15),
        "PANIC": _clip((-short_z - 2.5) / 1.5) * _clip((vol_pct - 0.6) / 0.2),
        "EUPHORIA": _clip((short_z - 2.5) / 1.5) * _clip((vol_pct - 0.6) / 0.2),
        "BREAKOUT": 0.0,
        "REVERSAL": 0.0,
    }
    broke = close > float(row.get("range_high", np.inf)) or close < float(row.get("range_low", -np.inf))
    if broke and np.isfinite(volume_ratio) and volume_ratio >= 1.5:
        scores["BREAKOUT"] = 0.6 + 0.4 * _clip((volume_ratio - 1.5) / 1.5)
    prior_efficiency = float(row.get("prior_efficiency", np.nan))
    prior_return = float(row.get("prior_return", np.nan))
    if np.isfinite(prior_efficiency) and prior_efficiency > 0.4 and np.isfinite(short_z):
        if np.sign(prior_return) != np.sign(short_z) and abs(short_z) > 2:
            scores["REVERSAL"] = _clip(0.5 + (abs(short_z) - 2) / 4) * _clip(prior_efficiency / 0.6)
    return scores


def choose(scores: dict[str, float]) -> tuple[str, float, dict[str, Any]]:
    ranked = sorted(scores.items(), key=lambda item: (-item[1], PRIORITY.get(item[0], 99)))
    (top, top_score), (second, second_score) = ranked[0], ranked[1]
    evidence: dict[str, Any] = {"secondary": second, "secondary_score": round(second_score, 3)}
    if top_score < 0.5:
        evidence["reason"] = "no regime has sufficient evidence"
        return "UNKNOWN", round(top_score, 3), evidence
    if top_score - second_score < 0.1 and frozenset({top, second}) in CONTRADICTORY:
        evidence["reason"] = f"contradictory evidence between {top} and {second}"
        return "UNKNOWN", round(top_score - second_score, 3), evidence
    return top, round(top_score, 3), evidence


class RegimeClassifier:
    def classify(self, frame: pd.DataFrame) -> RegimeResult:
        if len(frame) < MIN_BARS:
            return RegimeResult("UNKNOWN", 0.0, {}, {"reason": "insufficient_history", "bars": len(frame)})
        data = regime_features(frame.sort_values("timestamp").reset_index(drop=True))
        row = data.iloc[-1]
        scores = score_row(row)
        regime, confidence, evidence = choose(scores)
        evidence.update({
            key: (round(float(row[key]), 6) if pd.notna(row[key]) else None)
            for key in ("efficiency", "ema_spread", "trend_return", "vol", "vol_pct", "volume_ratio", "short_z")
        })
        return RegimeResult(regime, confidence, {k: round(v, 3) for k, v in scores.items()}, evidence)

    def classify_series(self, frame: pd.DataFrame) -> pd.Series:
        """Per-bar regime using only information available at each bar (no look-ahead)."""
        data = regime_features(frame.reset_index(drop=True))
        labels = []
        for position, (_, row) in enumerate(data.iterrows()):
            labels.append("UNKNOWN" if position < MIN_BARS - 1 else choose(score_row(row))[0])
        return pd.Series(labels, index=frame.index)


def candles_frame(rows: list[MarketCandle]) -> pd.DataFrame:
    frame = pd.DataFrame([{
        "timestamp": row.timestamp, "open": row.open, "high": row.high, "low": row.low,
        "close": row.close, "volume": row.volume,
    } for row in rows])
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def load_candles(session: Session, exchange: str, symbol: str, timeframe: str, limit: int = 300,
                 until: datetime | None = None) -> pd.DataFrame:
    """Latest `limit` candles; with `until`, only candles that opened at or before it (point in time)."""
    query = select(MarketCandle).where(
        MarketCandle.exchange == exchange, MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe,
    )
    if until is not None:
        query = query.where(MarketCandle.timestamp <= until)
    rows = session.scalars(query.order_by(desc(MarketCandle.timestamp)).limit(limit)).all()
    return candles_frame(list(reversed(rows)))


class RegimeService:
    """Persists regime history and flags transitions; one record per closed candle."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.classifier = RegimeClassifier()

    def latest(self, exchange: str, symbol: str, timeframe: str) -> MarketRegimeRecord | None:
        return self.session.scalar(select(MarketRegimeRecord).where(
            MarketRegimeRecord.exchange == exchange, MarketRegimeRecord.symbol == symbol,
            MarketRegimeRecord.timeframe == timeframe,
        ).order_by(desc(MarketRegimeRecord.candle_timestamp)))

    def update(
        self, exchange: str, symbol: str, timeframe: str, frame: pd.DataFrame | None = None,
    ) -> tuple[MarketRegimeRecord | None, RegimeResult]:
        data = frame if frame is not None else load_candles(self.session, exchange, symbol, timeframe)
        result = self.classifier.classify(data)
        if data.empty:
            return None, result
        candle_at: datetime = TimeService.ensure_utc(pd.Timestamp(data["timestamp"].iloc[-1]).to_pydatetime())
        previous = self.latest(exchange, symbol, timeframe)
        if previous and TimeService.ensure_utc(previous.candle_timestamp) >= candle_at:
            return previous, result
        record = MarketRegimeRecord(
            exchange=exchange, symbol=symbol, timeframe=timeframe, regime=result.regime,
            confidence=result.confidence, evidence={**result.evidence, "scores": result.scores},
            previous_regime=previous.regime if previous else None,
            is_transition=bool(previous and previous.regime != result.regime),
            candle_timestamp=candle_at,
        )
        self.session.add(record)
        self.session.flush()
        return record, result
