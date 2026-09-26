from typing import Any, Literal

from pydantic import BaseModel, Field


class ResearchHypothesisCreate(BaseModel):
    statement: str = Field(min_length=5, max_length=1000)
    description: str = Field("", max_length=4000)
    spec: dict[str, Any]
    market_conditions: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    exchange: str | None = Field(None, pattern=r"^[a-z]{2,20}$")


class HypothesisDecision(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class ChallengerCreate(BaseModel):
    parameters: dict[str, float] = Field(default_factory=dict)
    spec_overrides: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field("", max_length=2000)


class AITaskRequest(BaseModel):
    task: Literal["market_context", "hypotheses", "interpret_event", "analyze_trade", "explain_degradation"]
    symbol: str = Field("BTCUSDT", pattern=r"^[A-Z0-9]{2,20}$")
    timeframe: str = Field("1h", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    exchange: str = Field("binance", pattern=r"^[a-z]{2,20}$")
    subject_id: str | None = Field(None, max_length=64)


class SafetyControlCreate(BaseModel):
    scope: Literal["EMERGENCY", "SYSTEM", "PAPER_TRADING", "NEW_ORDERS", "STRATEGY", "ASSET"]
    target: str = Field("*", max_length=100)
    reason: str = Field(min_length=3, max_length=1000)


class SimilarityRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,20}$")
    timeframe: str = Field("1h", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    exchange: str = Field("binance", pattern=r"^[a-z]{2,20}$")
    same_symbol_only: bool = False
    limit: int = Field(10, ge=1, le=50)
    horizon_bars: int = Field(12, ge=1, le=500)


class ScanRequest(BaseModel):
    exchange: str | None = Field(None, pattern=r"^[a-z]{2,20}$")
    timeframe: str = Field("1h", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    symbols: list[str] = Field(default_factory=list, max_length=200)
