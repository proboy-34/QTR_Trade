import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.global_services.strategy_repository import StrategyRepository
from app.models import Order, PortfolioSnapshot, ValidationResult
from app.trading.pipeline import MarketSnapshot, TradingPipeline


def payload(): return {"name":"Test trend","symbol":"BTCUSDT","timeframe":"1h","version":{"parameters":{"fast":5,"slow":20},"entry_rules":[{"rule":"cross"}],"exit_rules":[{"rule":"reverse"}],"filters":{},"risk_assumptions":{},"documentation":"test"}}

def test_activation_requires_pass_validation(session):
    repository=StrategyRepository(session); strategy=repository.create(payload())
    repository.transition(strategy.id,"under_validation","ready for validation")
    with pytest.raises(SafetyError): repository.transition(strategy.id,"approved","missing pass")
    version=strategy.versions[0]
    session.add(ValidationResult(strategy_version_id=version.id,method="walk_forward",result="PASS",metrics={},rules={}))
    session.commit(); repository.transition(strategy.id,"approved","passed checks")
    assert repository.transition(strategy.id,"active","operator activation").status == "active"

def test_active_version_is_immutable(session):
    repository=StrategyRepository(session); strategy=repository.create(payload()); repository.transition(strategy.id,"under_validation","review")
    session.add(ValidationResult(strategy_version_id=strategy.versions[0].id,method="oos",result="PASS",metrics={},rules={})); session.commit()
    repository.transition(strategy.id,"approved","passed"); repository.transition(strategy.id,"active","activate")
    with pytest.raises(SafetyError): repository.add_version(strategy.id,payload()["version"])


@pytest.mark.asyncio
async def test_retired_strategy_is_not_selected_by_decision(session):
    repository = StrategyRepository(session)
    strategy = repository.create(payload())
    repository.transition(strategy.id, "under_validation", "review")
    session.add(ValidationResult(
        strategy_version_id=strategy.versions[0].id, method="walk_forward", result="PASS",
        metrics={}, rules={},
    ))
    session.commit()
    repository.transition(strategy.id, "approved", "passed")
    repository.transition(strategy.id, "active", "activate")
    repository.transition(strategy.id, "retired", "retire")
    session.add(PortfolioSnapshot(
        equity=100_000, available_balance=100_000, exposure=0, margin_used=0,
        daily_pnl=0, drawdown=0,
    ))
    session.commit()
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(MarketSnapshot(
        "BTCUSDT", 65_000, 2_000, .2, 0, "trending", "BULLISH"
    ))
    assert result["outcome"] == "WAIT"
    assert session.scalar(select(func.count()).select_from(Order)) == 0
