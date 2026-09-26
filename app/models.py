from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import TimeService
from app.db import Base

PRICE = Numeric(30, 12)
QUANTITY = Numeric(30, 12)
MONEY = Numeric(30, 10)
RATE = Numeric(20, 12)


def new_id() -> str:
    return str(uuid4())


class StrategyStatus(StrEnum):
    DRAFT = "draft"
    UNDER_VALIDATION = "under_validation"
    APPROVED = "approved"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"
    UNDER_REVIEW = "under_review"
    PAPER_TESTING = "paper_testing"
    READY_FOR_REVIEW = "ready_for_review"


class HypothesisStage(StrEnum):
    IDEA = "IDEA"
    RESEARCH = "RESEARCH"
    BACKTESTING = "BACKTESTING"
    OOS_VALIDATION = "OOS_VALIDATION"
    WALK_FORWARD = "WALK_FORWARD"
    ROBUSTNESS = "ROBUSTNESS"
    PAPER_TESTING = "PAPER_TESTING"
    PAPER_VALIDATED = "PAPER_VALIDATED"
    CANDIDATE = "CANDIDATE"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"
    ARCHIVED = "ARCHIVED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=TimeService.now, onupdate=TimeService.now
    )


class ResearchPlan(Base, TimestampMixin):
    __tablename__ = "research_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    objective: Mapped[str] = mapped_column(String(500))
    market: Mapped[str] = mapped_column(String(50), default="crypto")
    exchange: Mapped[str] = mapped_column(String(30), default="paper")
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    date_range: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dataset: Mapped[str] = mapped_column(String(100), default="demo_ohlcv")
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    expected_outputs: Mapped[list[str]] = mapped_column(JSON, default=list)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    experiment_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(30), default="planned", index=True)


class Experiment(Base, TimestampMixin):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    research_plan_id: Mapped[str | None] = mapped_column(ForeignKey("research_plans.id"))
    name: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), index=True)
    strategy_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_ms: Mapped[int | None] = mapped_column(Integer)
    reproducibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Dataset(Base, TimestampMixin):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(50))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    freshness_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class MarketCandle(Base):
    __tablename__ = "market_data"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(QUANTITY)
    funding_rate: Mapped[Decimal | None] = mapped_column(RATE)
    open_interest: Mapped[Decimal | None] = mapped_column(MONEY)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_market_candle_unique", "exchange", "symbol", "timeframe", "timestamp", unique=True),)


class Observation(Base, TimestampMixin):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiments.id"))
    title: Mapped[str] = mapped_column(String(300))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    conclusion: Mapped[str] = mapped_column(Text, default="")


class Hypothesis(Base, TimestampMixin):
    __tablename__ = "hypotheses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("observations.id"))
    statement: Mapped[str] = mapped_column(Text)
    test_definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[str] = mapped_column(String(30), default="untested")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(30), default=HypothesisStage.IDEA, index=True)
    origin: Mapped[str] = mapped_column(String(30), default="operator", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    market_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    timeframes: Mapped[list[str]] = mapped_column(JSON, default=list)
    variables: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assumptions: Mapped[list[str]] = mapped_column(JSON, default=list)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    strategy_id: Mapped[str | None] = mapped_column(String(36), index=True)
    parent_strategy_id: Mapped[str | None] = mapped_column(String(36), index=True)
    ai_artifact_id: Mapped[str | None] = mapped_column(String(36), index=True)
    stage_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    trials: Mapped[int] = mapped_column(Integer, default=0)


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    market: Mapped[str] = mapped_column(String(50), default="crypto")
    exchange: Mapped[str] = mapped_column(String(30), default="paper")
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default=StrategyStatus.DRAFT, index=True)
    author: Mapped[str] = mapped_column(String(100), default="QTR")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    origin: Mapped[str] = mapped_column(String(30), default="operator")
    parent_strategy_id: Mapped[str | None] = mapped_column(String(36), index=True)
    hypothesis_id: Mapped[str | None] = mapped_column(String(36), index=True)
    health_status: Mapped[str] = mapped_column(String(30), default="INSUFFICIENT_DATA", index=True)
    health_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    versions: Mapped[list["StrategyVersion"]] = relationship(back_populates="strategy")


class StrategyVersion(Base, TimestampMixin):
    __tablename__ = "strategy_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    entry_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    exit_rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk_assumptions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    documentation: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[Strategy] = relationship(back_populates="versions")
    __table_args__ = (Index("ix_strategy_version_unique", "strategy_id", "version", unique=True),)


class ValidationResult(Base, TimestampMixin):
    __tablename__ = "strategy_validation_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), index=True)
    method: Mapped[str] = mapped_column(String(50))
    result: Mapped[str] = mapped_column(String(20), index=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")


class StatusHistory(Base):
    __tablename__ = "strategy_status_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class MarketEvent(Base, TimestampMixin):
    __tablename__ = "market_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category: Mapped[str] = mapped_column(String(50), index=True)
    event_type: Mapped[str] = mapped_column(String(50), default="OTHER", index=True)
    title: Mapped[str] = mapped_column(String(300), default="Market intelligence event")
    severity: Mapped[str] = mapped_column(String(20), index=True)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(200))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    affected_assets: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="scheduled")
    provider: Mapped[str] = mapped_column(String(50), default="manual", index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    dedup_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # SOURCE_VERIFIED: fetched from a configured provider with a traceable reference.
    # OPERATOR_ENTERED: manual entry. UNVERIFIED: no traceable source. Never AI-created.
    verification_status: Mapped[str] = mapped_column(String(30), default="OPERATOR_ENTERED", index=True)
    relevance: Mapped[float] = mapped_column(Float, default=0)
    expected_value: Mapped[str | None] = mapped_column(String(50))
    actual_value: Mapped[str | None] = mapped_column(String(50))
    previous_value: Mapped[str | None] = mapped_column(String(50))
    surprise: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(20))
    raw_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # When the information became knowable. Decisions and research may only use events with
    # available_at <= decision time (news: publication; macro: release time).
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(String(30), default="NORMALIZED", index=True)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    outcome: Mapped[str] = mapped_column(String(10), index=True)
    market_context: Mapped[dict[str, Any]] = mapped_column(JSON)
    evaluations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    reasoning: Mapped[list[str]] = mapped_column(JSON, default=list)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    selected_opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), index=True)
    exchange: Mapped[str | None] = mapped_column(String(30))
    timeframe: Mapped[str | None] = mapped_column(String(10), index=True)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Structured trade decision: thesis, supporting/contradicting evidence, invalidation, plan, AI review.
    rationale: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class TradeIntent(Base):
    __tablename__ = "trade_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), index=True)
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(10))
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    # Legacy nullable hints retained for migration compatibility. Decision never sets them;
    # final protective levels belong exclusively to ExecutionPlan.
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    confidence: Mapped[Decimal] = mapped_column(RATE)
    status: Mapped[str] = mapped_column(String(30), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class ExecutionPlan(Base):
    __tablename__ = "execution_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trade_intent_id: Mapped[str] = mapped_column(ForeignKey("trade_intents.id"), unique=True)
    exchange: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(30))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    order_type: Mapped[str] = mapped_column(String(20), default="MARKET")
    status: Mapped[str] = mapped_column(String(30), default="approved")
    risk_amount: Mapped[Decimal] = mapped_column(MONEY)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE)
    take_profit: Mapped[Decimal] = mapped_column(PRICE)
    leverage: Mapped[Decimal] = mapped_column(RATE, default=Decimal("1"))
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class Position(Base, TimestampMixin):
    __tablename__ = "positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    current_price: Mapped[Decimal] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE)
    take_profit: Mapped[Decimal] = mapped_column(PRICE)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    exit_reason: Mapped[str | None] = mapped_column(String(100))
    highest_price: Mapped[Decimal | None] = mapped_column(PRICE)
    lowest_price: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    # Execution venue: paper | testnet. Paper, testnet and live state are never mixed.
    venue: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # REST monitoring: the last closed candle already applied to this position (restart-safe).
    timeframe: Mapped[str | None] = mapped_column(String(10))
    market_exchange: Mapped[str | None] = mapped_column(String(30))
    last_evaluated_candle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    max_holding_bars: Mapped[int | None] = mapped_column(Integer)
    decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    slippage_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    exit_price: Mapped[Decimal | None] = mapped_column(PRICE)


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    execution_plan_id: Mapped[str] = mapped_column(ForeignKey("execution_plans.id"), index=True)
    position_id: Mapped[str | None] = mapped_column(ForeignKey("positions.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    exchange_order_id: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(10))
    order_type: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    fill_quantity: Mapped[Decimal] = mapped_column(QUANTITY, default=Decimal("0"))
    average_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(30), index=True)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Provenance: paper fills are SIMULATED against real quotes; they are never exchange fills.
    execution_mode: Mapped[str] = mapped_column(String(20), default="PAPER", index=True)
    market_data_source: Mapped[str] = mapped_column(String(30), default="UNKNOWN")
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    bid: Mapped[Decimal | None] = mapped_column(PRICE)
    ask: Mapped[Decimal | None] = mapped_column(PRICE)
    quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slippage_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    execution_key: Mapped[str] = mapped_column(String(100), unique=True)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(MONEY)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    available_balance: Mapped[Decimal] = mapped_column(MONEY)
    exposure: Mapped[Decimal] = mapped_column(RATE)
    margin_used: Mapped[Decimal] = mapped_column(MONEY)
    daily_pnl: Mapped[Decimal] = mapped_column(MONEY)
    drawdown: Mapped[Decimal] = mapped_column(RATE)
    venue: Mapped[str] = mapped_column(String(20), default="paper", index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    type: Mapped[str] = mapped_column(String(80), index=True)
    component: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(80), default="qtr")
    version: Mapped[int] = mapped_column(Integer, default=1)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class IntegrationMetadata(Base, TimestampMixin):
    __tablename__ = "exchange_credentials_metadata"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(30), unique=True)
    configured: Mapped[bool] = mapped_column(Boolean, default=False)
    connection_status: Mapped[str] = mapped_column(String(30), default="not_configured")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Asset(Base, TimestampMixin):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    base_asset: Mapped[str] = mapped_column(String(20))
    quote_asset: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(30), unique=True)
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    step_size: Mapped[Decimal] = mapped_column(QUANTITY)
    min_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    min_notional: Mapped[Decimal] = mapped_column(MONEY)
    contract_type: Mapped[str] = mapped_column(String(30), default="spot")
    exchange_mappings: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)


class MarketContextRecord(Base):
    __tablename__ = "market_contexts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    price: Mapped[Decimal] = mapped_column(PRICE)
    regime: Mapped[str] = mapped_column(String(30), index=True)
    direction: Mapped[str] = mapped_column(String(20))
    volatility: Mapped[float] = mapped_column(Float)
    liquidity: Mapped[str] = mapped_column(String(20))
    funding: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0"))
    previous_regime: Mapped[str | None] = mapped_column(String(30))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    exchange: Mapped[str | None] = mapped_column(String(30), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(10), index=True)
    market_regime: Mapped[str | None] = mapped_column(String(30), index=True)
    regime_confidence: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class Opportunity(Base, TimestampMixin):
    __tablename__ = "opportunities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    market_context_id: Mapped[str] = mapped_column(ForeignKey("market_contexts.id"), index=True)
    # Scanner opportunities are strategy-agnostic observations; decision opportunities carry a version.
    strategy_version_id: Mapped[str | None] = mapped_column(ForeignKey("strategy_versions.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(20), default="decision", index=True)
    exchange: Mapped[str | None] = mapped_column(String(30))
    timeframe: Mapped[str | None] = mapped_column(String(10))
    signals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    regime: Mapped[str | None] = mapped_column(String(30))
    dedup_key: Mapped[str | None] = mapped_column(String(120), index=True)
    rank_score: Mapped[float] = mapped_column(Float, default=0)
    rank_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observations: Mapped[int] = mapped_column(Integer, default=1)


class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trade_intent_id: Mapped[str] = mapped_column(ForeignKey("trade_intents.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(20), index=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class PositionEvent(Base):
    __tablename__ = "position_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(30), index=True)
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    reason: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class ExecutionReport(Base):
    __tablename__ = "execution_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), unique=True)
    exchange: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    fill_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    average_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    execution_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class ReconciliationIssue(Base, TimestampMixin):
    __tablename__ = "reconciliation_issues"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[str] = mapped_column(String(100), index=True)
    issue_type: Mapped[str] = mapped_column(String(50), index=True)
    internal_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    exchange_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    resolution: Mapped[str] = mapped_column(Text, default="")


class AssetInstrument(Base, TimestampMixin):
    __tablename__ = "asset_instruments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    exchange_symbol: Mapped[str] = mapped_column(String(80), index=True)
    contract_type: Mapped[str] = mapped_column(String(30), default="spot")
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    step_size: Mapped[Decimal] = mapped_column(QUANTITY)
    min_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    min_notional: Mapped[Decimal] = mapped_column(MONEY)
    price_precision: Mapped[int] = mapped_column(Integer)
    quantity_precision: Mapped[int] = mapped_column(Integer)
    trading_status: Mapped[str] = mapped_column(String(30), default="TRADING", index=True)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index("ix_asset_instrument_unique", "exchange", "exchange_symbol", unique=True),
    )


class MarketDataBackfill(Base, TimestampMixin):
    __tablename__ = "market_data_backfills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    progress: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0"))
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    batch_limit: Mapped[int] = mapped_column(Integer, default=500)


class MarketDataValidationFailure(Base):
    __tablename__ = "market_data_validation_failures"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    backfill_id: Mapped[str | None] = mapped_column(ForeignKey("market_data_backfills.id"), index=True)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class JobExecution(Base):
    __tablename__ = "job_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_name: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class LivePaperSession(Base):
    __tablename__ = "live_paper_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(30), default="STARTING", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_candle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candles_received: Mapped[int] = mapped_column(Integer, default=0)
    decisions_run: Mapped[int] = mapped_column(Integer, default=0)
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AssetEligibility(Base):
    """Immutable per-run eligibility verdict for one instrument."""

    __tablename__ = "asset_eligibility"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    base_asset: Mapped[str] = mapped_column(String(20))
    quote_asset: Mapped[str] = mapped_column(String(20))
    eligible: Mapped[bool] = mapped_column(Boolean, index=True)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rank: Mapped[int | None] = mapped_column(Integer)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exchange: Mapped[str] = mapped_column(String(30), index=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10), index=True)
    regime: Mapped[str] = mapped_column(String(30), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    previous_regime: Mapped[str | None] = mapped_column(String(30))
    is_transition: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    candle_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)
    __table_args__ = (
        Index("ix_market_regime_unique", "exchange", "symbol", "timeframe", "candle_timestamp", unique=True),
    )


class AICallRecord(Base):
    __tablename__ = "ai_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    model: Mapped[str] = mapped_column(String(80))
    task_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    request_hash: Mapped[str] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class AIArtifact(Base):
    """AI output kept separate from facts. Claims carry their own verification status."""

    __tablename__ = "ai_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ai_call_id: Mapped[str | None] = mapped_column(ForeignKey("ai_calls.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(50), index=True)
    subject_type: Mapped[str] = mapped_column(String(30), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(100), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    proposals: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    context_refs: Mapped[list[str]] = mapped_column(JSON, default=list)
    verification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_version: Mapped[str] = mapped_column(String(20), default="v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class TradeMemory(Base):
    """Immutable closed-trade record. One row per closed position; never updated."""

    __tablename__ = "trade_memory"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    leverage: Mapped[Decimal] = mapped_column(RATE, default=Decimal("1"))
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    exit_price: Mapped[Decimal] = mapped_column(PRICE)
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE)
    fees: Mapped[Decimal] = mapped_column(MONEY)
    slippage_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY)
    return_pct: Mapped[float] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mae_pct: Mapped[float | None] = mapped_column(Float)
    mfe_pct: Mapped[float | None] = mapped_column(Float)
    holding_seconds: Mapped[int] = mapped_column(Integer)
    exit_reason: Mapped[str] = mapped_column(String(100))
    strategy_version_id: Mapped[str] = mapped_column(String(36), index=True)
    decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    trade_intent_id: Mapped[str | None] = mapped_column(String(36))
    execution_plan_id: Mapped[str | None] = mapped_column(String(36))
    timeframe: Mapped[str | None] = mapped_column(String(10))
    regime_at_entry: Mapped[str | None] = mapped_column(String(30), index=True)
    regime_at_exit: Mapped[str | None] = mapped_column(String(30))
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    stop_loss: Mapped[Decimal] = mapped_column(PRICE)
    take_profit: Mapped[Decimal] = mapped_column(PRICE)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lineage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)


class PostTradeAnalysis(Base):
    __tablename__ = "post_trade_analyses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trade_memory_id: Mapped[str] = mapped_column(ForeignKey("trade_memory.id"), unique=True)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actual: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    thesis_correct: Mapped[bool | None] = mapped_column(Boolean)
    entry_quality: Mapped[str] = mapped_column(String(30))
    exit_quality: Mapped[str] = mapped_column(String(30))
    factors: Mapped[list[str]] = mapped_column(JSON, default=list)
    lesson: Mapped[str] = mapped_column(Text, default="")
    knowledge_entry_id: Mapped[str | None] = mapped_column(String(36))
    ai_artifact_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class KnowledgeEntry(Base):
    """Queryable research memory. source_type separates system facts from AI interpretation."""

    __tablename__ = "knowledge_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    symbol: Mapped[str | None] = mapped_column(String(30), index=True)
    timeframe: Mapped[str | None] = mapped_column(String(10))
    regime: Mapped[str | None] = mapped_column(String(30), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default="SYSTEM_DERIVED", index=True)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    dedup_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class CounterfactualEvaluation(Base):
    __tablename__ = "counterfactuals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    exchange: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    decision_outcome: Mapped[str] = mapped_column(String(30))
    rejection_category: Mapped[str] = mapped_column(String(40), index=True)
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    reference_price: Mapped[Decimal] = mapped_column(PRICE)
    reference_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_bars: Mapped[int] = mapped_column(Integer)
    forward_return_pct: Mapped[float | None] = mapped_column(Float)
    max_up_pct: Mapped[float | None] = mapped_column(Float)
    max_down_pct: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str | None] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class StrategyHealthRecord(Base):
    __tablename__ = "strategy_health"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"), index=True)
    strategy_version_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), index=True)
    historical: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    recent: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    by_regime: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class DiscrepancyReport(Base):
    __tablename__ = "discrepancy_reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    strategy_version_id: Mapped[str] = mapped_column(String(36), index=True)
    backtest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    paper: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    differences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    explanations: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class SafetyControl(Base):
    """Kill-switch record. Automatic triggers stay active until an operator clears them."""

    __tablename__ = "safety_controls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(30), index=True)
    target: Mapped[str] = mapped_column(String(100), default="*", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(50), default="OPERATOR")
    source: Mapped[str] = mapped_column(String(30), default="operator")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleared_by: Mapped[str | None] = mapped_column(String(100))


class ProviderCheck(Base):
    """One real connectivity/capability check against an external provider. Never synthesized."""

    __tablename__ = "provider_checks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    provider: Mapped[str] = mapped_column(String(30), index=True)
    check: Mapped[str] = mapped_column(String(60))
    endpoint: Mapped[str] = mapped_column(String(200))
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    # OK | FAILED | NOT_CONFIGURED | NOT_AVAILABLE_ON_PLAN | SKIPPED
    result: Mapped[str] = mapped_column(String(30), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)


class MacroObservation(Base):
    """Point-in-time macro value. available_at is when the value became public (vintage start)."""

    __tablename__ = "macro_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(30), default="fred", index=True)
    series_id: Mapped[str] = mapped_column(String(40), index=True)
    observation_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    realtime_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    realtime_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now)
    units: Mapped[str | None] = mapped_column(String(80))
    title: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        Index("ix_macro_vintage_unique", "source", "series_id", "observation_date", "realtime_start", unique=True),
    )


class ProcessedCandle(Base):
    """Idempotency ledger of the REST closed-candle loop: one row per exchange/symbol/timeframe/open time."""

    __tablename__ = "processed_candles"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "timeframe", "candle_open_time",
                                       name="uq_processed_candle"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    exchange: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(30), index=True)
    timeframe: Mapped[str] = mapped_column(String(10))
    candle_open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    candle_close_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="CLAIMED", index=True)
    outcome: Mapped[str | None] = mapped_column(String(20))
    decision_id: Mapped[str | None] = mapped_column(String(36), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=TimeService.now, index=True)
