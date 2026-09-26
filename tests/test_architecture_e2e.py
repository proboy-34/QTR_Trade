
import numpy as np
import pandas as pd
import pytest
from paper_fixtures import LEGACY, add_passes
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.events import EventBus
from app.db import Base
from app.global_services.backtest import BacktestEngine
from app.global_services.strategy_repository import StrategyRepository
from app.models import (
    Decision,
    ExecutionPlan,
    ExecutionReport,
    Experiment,
    Hypothesis,
    Observation,
    Order,
    PortfolioSnapshot,
    Position,
    ResearchPlan,
    Strategy,
    TradeIntent,
    ValidationResult,
)
from app.research.experiments import ExperimentManager
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.positions import PositionManager


def historical_data(rows: int = 180) -> pd.DataFrame:
    close = 100 + np.sin(np.arange(rows) / 6) * 8 + np.linspace(0, 12, rows)
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC"),
        "open": close,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 1000,
    })


@pytest.mark.asyncio
async def test_complete_research_to_position_close_evidence_chain(session):
    plan = ResearchPlan(
        objective="Validate deterministic momentum", symbol="BTCUSDT", timeframe="1h",
        scope={"market": "spot"}, experiment_plan={"method": "backtest"},
    )
    session.add(plan)
    session.commit()
    experiment = ExperimentManager(session).create(
        "Momentum evidence", {"symbol": "BTCUSDT", "fast": 5, "slow": 20}, plan.id
    )
    observation = Observation(
        experiment_id=experiment.id, title="Momentum persistence",
        evidence={"sample": "2025"}, conclusion="Crossover warrants deterministic testing",
    )
    session.add(observation)
    session.flush()
    hypothesis = Hypothesis(
        observation_id=observation.id, statement="Fast EMA crossover has positive expectancy",
        test_definition={"fast": 5, "slow": 20}, result="supported",
    )
    session.add(hypothesis)
    session.commit()
    backtest = BacktestEngine().run(historical_data(), fast=5, slow=20)
    ExperimentManager(session).execute(experiment, lambda _: backtest)

    repository = StrategyRepository(session)
    strategy = repository.create({
        "name": "Architecture E2E",
        "description": "Full evidence chain",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "version": {
            "parameters": {"fast": 5, "slow": 20},
            "entry_rules": [{"left": "ema_fast", "operator": "crosses_above", "right": "ema_slow"}],
            "exit_rules": [{"left": "ema_fast", "operator": "crosses_below", "right": "ema_slow"}],
            "filters": {"regime": ["trending"]},
            "risk_assumptions": {"risk_fraction": .01},
            "documentation": "Reproducible architecture end-to-end strategy evidence.",
        },
    })
    repository.transition(strategy.id, "under_validation", "research review passed")
    version = strategy.versions[0]
    session.add(ValidationResult(
        strategy_version_id=version.id, method="backtest", result="PASS",
        metrics=backtest["metrics"], rules={"lookahead": "next_bar"},
        configuration={"fast": 5, "slow": 20}, notes="deterministic test",
    ))
    session.commit()
    add_passes(session, version.id)  # the remaining research methods (fixture evidence)
    session.commit()
    repository.transition(strategy.id, "approved", "validation pass")
    repository.transition(strategy.id, "active", "operator activation")
    session.add(PortfolioSnapshot(
        equity=100_000, available_balance=100_000, exposure=0, margin_used=0,
        daily_pnl=0, drawdown=0,
    ))
    session.commit()

    result = await TradingPipeline(session, Settings(**LEGACY), EventBus()).evaluate(
        MarketSnapshot("BTCUSDT", 65_000, 2_000, .3, .0001, "trending", "BULLISH")
    )
    assert result["risk_outcome"] == "APPROVED"
    intent = session.get(TradeIntent, result["trade_intent_id"])
    execution_plan = session.get(ExecutionPlan, result["execution_plan_id"])
    assert intent.stop_loss is None and intent.take_profit is None
    assert execution_plan.stop_loss > 0 and execution_plan.take_profit > execution_plan.stop_loss
    position = session.get(Position, result["position_id"])
    PositionManager(session).mark(position, 66_000)
    PositionManager(session).close(position, 67_000, "strategy exit")
    assert position.status == "CLOSED"
    assert session.scalar(select(func.count()).select_from(ExecutionReport)) == 1
    for model in (ResearchPlan, Experiment, Observation, Hypothesis, Strategy, ValidationResult, Decision, TradeIntent, ExecutionPlan, Order, Position):
        assert session.scalar(select(func.count()).select_from(model)) >= 1


@pytest.mark.asyncio
async def test_no_trade_creates_no_downstream_records(session):
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(
        MarketSnapshot("ETHUSDT", 3_000, 100, .2, 0, "sideways", "BEARISH")
    )
    assert result["outcome"] == "WAIT"
    for model in (TradeIntent, ExecutionPlan, Order, Position):
        assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_ignored(session):
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(
        MarketSnapshot("BTCUSDT", -1, 100, .2, 0, "trending", "BULLISH")
    )
    assert result["outcome"] == "IGNORE"
    assert session.scalar(select(func.count()).select_from(TradeIntent)) == 0


def test_restart_persists_orders_and_positions(tmp_path):
    database = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as first:
        position = Position(
            strategy_version_id="version", symbol="BTCUSDT", side="BUY", quantity=1,
            entry_price=100, current_price=100, stop_loss=95, take_profit=110, status="OPEN",
        )
        first.add(position)
        first.commit()
        position_id = position.id
    engine.dispose()
    restarted = create_engine(f"sqlite:///{database}")
    with sessionmaker(bind=restarted)() as second:
        restored = second.get(Position, position_id)
        assert restored is not None and restored.status == "OPEN" and restored.quantity == 1

