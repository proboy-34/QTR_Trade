"""Safe, declarative strategy specification.

Strategies (including AI-proposed ones) are data, never code. A specification may only
reference whitelisted features and operators; it is compiled by QTR's own functions.
The format is the existing StrategyVersion entry/exit rule shape, so repository
versions and research specifications stay one system.
"""

import re
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.global_services.features import atr, ema, rsi, sma, vwap

TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
OPERATORS = {">": "gt", "<": "lt", ">=": "gte", "<=": "lte", "gt": "gt", "lt": "lt", "gte": "gte",
             "lte": "lte", "crosses_above": "crosses_above", "crosses_below": "crosses_below"}
LEGACY_ALIASES = {"ema_fast": "ema:{fast}", "ema_slow": "ema:{slow}", "sma_20": "sma:20", "rsi": "rsi:14",
                  "price": "close", "vwap": "vwap"}
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]{0,30})\}")
MAX_PERIOD = 500


def _volume_ratio(frame: pd.DataFrame, period: int) -> pd.Series:
    return frame["volume"] / frame["volume"].shift(1).rolling(period).median()


def _efficiency(frame: pd.DataFrame, period: int) -> pd.Series:
    close = frame["close"]
    return (close - close.shift(period)).abs() / close.diff().abs().rolling(period).sum().replace(0, np.nan)


PERIOD_FEATURES: dict[str, Callable[[pd.DataFrame, int], pd.Series]] = {
    "ema": lambda f, n: ema(f["close"], n),
    "sma": lambda f, n: sma(f["close"], n),
    "rsi": lambda f, n: rsi(f["close"], n),
    "atr_pct": lambda f, n: atr(f, n) / f["close"],
    "return": lambda f, n: f["close"].pct_change(n),
    "volatility": lambda f, n: f["close"].pct_change().rolling(n).std(),
    "volume_ratio": _volume_ratio,
    # Prior-bar extremes: the current bar is excluded so breakouts are not look-ahead.
    "highest": lambda f, n: f["high"].shift(1).rolling(n).max(),
    "lowest": lambda f, n: f["low"].shift(1).rolling(n).min(),
    "efficiency": _efficiency,
}
PLAIN_FEATURES: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "close": lambda f: f["close"], "open": lambda f: f["open"], "high": lambda f: f["high"],
    "low": lambda f: f["low"], "volume": lambda f: f["volume"], "vwap": vwap,
}


class Condition(BaseModel):
    left: str | float
    operator: str
    right: str | float

    @field_validator("operator")
    @classmethod
    def known_operator(cls, value: str) -> str:
        if value not in OPERATORS:
            raise ValueError(f"unsupported operator {value!r}")
        return OPERATORS[value]


class RiskSpec(BaseModel):
    stop_loss_pct: float = Field(0.02, gt=0.0005, le=0.25)
    take_profit_pct: float = Field(0.04, gt=0.0005, le=1.0)
    max_holding_bars: int | None = Field(None, ge=1, le=10_000)


class StrategySpec(BaseModel):
    side: Literal["long"] = "long"
    entry: list[Condition] = Field(min_length=1, max_length=8)
    exit: list[Condition] = Field(default_factory=list, max_length=8)
    risk: RiskSpec = Field(default_factory=lambda: RiskSpec.model_validate({}))
    timeframes: list[str] = Field(min_length=1, max_length=6)
    universe: list[str] = Field(min_length=1, max_length=100)
    parameters: dict[str, float] = Field(default_factory=dict)
    regimes: list[str] = Field(default_factory=list)

    @field_validator("timeframes")
    @classmethod
    def known_timeframes(cls, value: list[str]) -> list[str]:
        unknown = [item for item in value if item not in TIMEFRAMES]
        if unknown:
            raise ValueError(f"unsupported timeframes {unknown}")
        return value

    @field_validator("universe")
    @classmethod
    def clean_universe(cls, value: list[str]) -> list[str]:
        cleaned = [item.upper() for item in value]
        if any(not re.fullmatch(r"[A-Z0-9]{2,20}", item) for item in cleaned):
            raise ValueError("universe entries must be exchange symbols or ELIGIBLE")
        return cleaned

    @model_validator(mode="after")
    def operands_resolve(self) -> "StrategySpec":
        for condition in [*self.entry, *self.exit]:
            for operand in (condition.left, condition.right):
                if isinstance(operand, str):
                    resolve_operand(operand, self.parameters)
        for name, value in self.parameters.items():
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,30}", name) or not np.isfinite(value):
                raise ValueError(f"invalid parameter {name}")
        return self

    @property
    def parameter_count(self) -> int:
        return len(self.parameters)


def resolve_operand(operand: str, parameters: dict[str, float]) -> tuple[str, int | None]:
    """Return (feature, period). Raises ValueError for anything not whitelisted."""
    text = LEGACY_ALIASES.get(operand.strip(), operand.strip())

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            raise ValueError(f"unknown parameter {name}")
        return str(int(parameters[name]))

    text = PLACEHOLDER.sub(substitute, text)
    if text in PLAIN_FEATURES:
        return text, None
    match = re.fullmatch(r"([a-z_]+):(\d{1,4})", text)
    if not match or match.group(1) not in PERIOD_FEATURES:
        raise ValueError(f"unsupported feature {operand!r}")
    period = int(match.group(2))
    if not 1 <= period <= MAX_PERIOD:
        raise ValueError(f"period out of range in {operand!r}")
    return match.group(1), period


class CompiledStrategy:
    def __init__(self, spec: StrategySpec) -> None:
        self.spec = spec

    def warmup(self) -> int:
        periods = [1]
        for condition in [*self.spec.entry, *self.spec.exit]:
            for operand in (condition.left, condition.right):
                if isinstance(operand, str):
                    _, period = resolve_operand(operand, self.spec.parameters)
                    periods.append(period or 1)
        return max(periods) * 3 + 2

    def _series(self, frame: pd.DataFrame, operand: str | float, cache: dict[str, pd.Series]) -> pd.Series:
        if not isinstance(operand, str):
            return pd.Series(float(operand), index=frame.index)
        feature, period = resolve_operand(operand, self.spec.parameters)
        key = f"{feature}:{period}"
        if key not in cache:
            cache[key] = PLAIN_FEATURES[feature](frame) if period is None else PERIOD_FEATURES[feature](frame, period)
        return cache[key]

    def _evaluate(self, frame: pd.DataFrame, condition: Condition, cache: dict[str, pd.Series]) -> pd.Series:
        left = self._series(frame, condition.left, cache)
        right = self._series(frame, condition.right, cache)
        op = condition.operator
        if op == "gt":
            result = left > right
        elif op == "lt":
            result = left < right
        elif op == "gte":
            result = left >= right
        elif op == "lte":
            result = left <= right
        elif op == "crosses_above":
            result = (left > right) & (left.shift(1) <= right.shift(1))
        else:
            result = (left < right) & (left.shift(1) >= right.shift(1))
        return result.fillna(False).astype(bool)

    def feature_snapshot(self, frame: pd.DataFrame) -> dict[str, Any]:
        """Values of exactly the features this specification uses, on the last bar of `frame`
        (and the bar before, which cross conditions compare against), plus each condition's result.
        Computed with the same code as the signals, on the same closed candles — nothing extra."""
        cache: dict[str, pd.Series] = {}
        values: dict[str, Any] = {}
        conditions: list[dict[str, Any]] = []

        def value(series: pd.Series, offset: int) -> float | None:
            if len(series) < offset:
                return None
            item = series.iloc[-offset]
            return round(float(item), 10) if pd.notna(item) else None

        for kind, items in (("entry", self.spec.entry), ("exit", self.spec.exit)):
            for condition in items:
                for operand in (condition.left, condition.right):
                    if isinstance(operand, str) and operand not in values:
                        series = self._series(frame, operand, cache)
                        values[operand] = {"last": value(series, 1), "previous": value(series, 2)}
                result = self._evaluate(frame, condition, cache)
                conditions.append({"kind": kind, "rule": f"{condition.left} {condition.operator} {condition.right}",
                                   "result": bool(result.iloc[-1]) if len(result) else False})
        return {"features": values, "conditions": conditions}

    def signals(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Entry/exit flags known at each bar's close. Execution must use the next bar."""
        cache: dict[str, pd.Series] = {}
        entry = pd.Series(True, index=frame.index)
        for condition in self.spec.entry:
            entry &= self._evaluate(frame, condition, cache)
        exit_signal = pd.Series(False, index=frame.index)
        for condition in self.spec.exit:
            exit_signal |= self._evaluate(frame, condition, cache)
        return pd.DataFrame({"entry": entry, "exit": exit_signal}, index=frame.index)


def parse_spec(payload: dict[str, Any]) -> StrategySpec:
    return StrategySpec.model_validate(payload)


def validation_errors(payload: Any) -> list[str]:
    try:
        StrategySpec.model_validate(payload)
        return []
    except ValidationError as exc:
        return [f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()]


def spec_from_version(version: Any, strategy: Any) -> StrategySpec | None:
    """Interpret a repository version as a specification; None for non-declarative legacy rules."""
    filters = version.filters or {}
    risk = version.risk_assumptions or {}
    payload = {
        "entry": version.entry_rules or [],
        "exit": version.exit_rules or [],
        "risk": {key: risk[key] for key in ("stop_loss_pct", "take_profit_pct", "max_holding_bars") if key in risk},
        "timeframes": [strategy.timeframe],
        "universe": filters.get("universe") or [strategy.symbol],
        "parameters": {key: value for key, value in (version.parameters or {}).items()
                       if isinstance(value, (int, float)) and not isinstance(value, bool)},
        "regimes": filters.get("market_regimes") or [],
    }
    try:
        return StrategySpec.model_validate(payload)
    except ValidationError:
        return None


def version_payload(spec: StrategySpec, documentation: str, extra_filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Repository version fields for a specification (immutable once stored)."""
    return {
        "parameters": dict(spec.parameters),
        "entry_rules": [condition.model_dump() for condition in spec.entry],
        "exit_rules": [condition.model_dump() for condition in spec.exit],
        "filters": {"universe": spec.universe, "market_regimes": spec.regimes, "timeframes": spec.timeframes,
                    **(extra_filters or {})},
        "risk_assumptions": spec.risk.model_dump(exclude_none=True),
        "documentation": documentation,
    }
