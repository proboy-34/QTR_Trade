from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field

from app.core.time import TimeService


class ResearchPlanCreate(BaseModel):
    objective: str
    market: str = "crypto"
    exchange: str = "paper"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    date_range: dict[str, Any] = Field(default_factory=dict)
    dataset: str = "demo_ohlcv"
    features: list[str] = Field(default_factory=lambda: ["ema", "rsi", "atr"])
    hypothesis: str = ""
    expected_outputs: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    experiment_plan: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(3, ge=1, le=5)


class StrategyVersionCreate(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=lambda: {"fast": 10, "slow": 30})
    entry_rules: list[dict[str, Any]] = Field(default_factory=list)
    exit_rules: list[dict[str, Any]] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    risk_assumptions: dict[str, Any] = Field(default_factory=dict)
    documentation: str = ""


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    market: str = "crypto"
    exchange: str = "paper"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    author: str = "QTR operator"
    version: StrategyVersionCreate


class TransitionRequest(BaseModel):
    status: str
    reason: str = Field(min_length=3)


class BacktestRequest(BaseModel):
    strategy_version_id: str | None = None
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    fast: int = Field(10, ge=2, le=100)
    slow: int = Field(30, ge=3, le=300)
    stop_loss_pct: float = Field(0.02, gt=0, le=0.25)
    take_profit_pct: float = Field(0.04, gt=0, le=1)
    fee_rate: float = Field(0.0004, ge=0, le=0.02)
    slippage_rate: float = Field(0.0002, ge=0, le=0.02)


class MarketSnapshotInput(BaseModel):
    symbol: str = "BTCUSDT"
    price: float = Field(gt=0)
    volume: float = Field(ge=0)
    volatility: float = Field(ge=0)
    funding: float = 0
    regime: str = "trending"
    direction: str = "BULLISH"
    liquidity: str = "STRONG"
    observed_at: datetime = Field(default_factory=TimeService.now)
    exchange: str = "paper"
    timeframe: str = "1h"
    force_signal: bool = False


class MarketEventCreate(BaseModel):
    category: str
    event_type: str = "OTHER"
    title: str
    severity: str
    event_at: datetime
    source: str
    source_url: str | None = None
    affected_assets: list[str]
    description: str
    status: str = "scheduled"


class ExperimentCreate(BaseModel):
    name: str
    research_plan_id: str | None = None
    configuration: dict[str, Any]


class ExperimentAction(BaseModel):
    action: str


class ObservationCreate(BaseModel):
    experiment_id: str | None = None
    title: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    conclusion: str = ""


class HypothesisCreate(BaseModel):
    observation_id: str | None = None
    statement: str
    test_definition: dict[str, Any] = Field(default_factory=dict)
    result: str = "untested"
    evidence: dict[str, Any] = Field(default_factory=dict)


class WalkForwardRequest(BaseModel):
    strategy_version_id: str
    dataset_id: str | None = None
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    train_size: int = Field(100, ge=32)
    validation_size: int = Field(40, ge=32)
    step_size: int = Field(40, ge=1)
    fast: int = Field(10, ge=2)
    slow: int = Field(30, ge=3)


class PositionAction(BaseModel):
    action: str
    price: float | None = Field(None, gt=0)
    quantity: float | None = Field(None, gt=0)
    reason: str = "operator action"


class ReconciliationRequest(BaseModel):
    exchange: str = "paper"
    orders: dict[str, dict[str, Any]] = Field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BackfillCreate(BaseModel):
    exchange: str = "paper"
    symbol: str = "BTCUSDT"
    timeframe: str = "1h"
    start_at: datetime
    end_at: datetime
    batch_limit: int = Field(500, ge=1, le=1000)


class LivePaperStart(BaseModel):
    provider: str = "binance"
    symbol: str = Field("BTCUSDT", pattern=r"^[A-Z0-9]{5,20}$")
    timeframe: str = Field("1h", pattern=r"^(1m|5m|15m|1h|4h|1d)$")
    symbols: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9]{5,20}$")]] = Field(default_factory=list, max_length=50)
