import hashlib

import pytest
from paper_fixtures import LEGACY, add_passes
from sqlalchemy import select

from app.core.config import Settings
from app.core.events import EventBus
from app.models import Order, PortfolioSnapshot, Position, Strategy, StrategyVersion
from app.trading.pipeline import MarketSnapshot, TradingPipeline


@pytest.mark.asyncio
async def test_end_to_end_paper_trade(session):
    strategy=Strategy(name="E2E",symbol="BTCUSDT",timeframe="1h",status="active")
    session.add(strategy); session.flush()
    version=StrategyVersion(strategy_id=strategy.id,version=1,parameters={},entry_rules=[{"rule":"bullish"}],exit_rules=[],filters={"regime":["trending"]},risk_assumptions={},documentation="e2e",content_hash=hashlib.sha256(b'e2e').hexdigest())
    session.add(version); session.flush(); add_passes(session, version.id)
    session.add(PortfolioSnapshot(equity=100000,available_balance=100000,exposure=0,margin_used=0,daily_pnl=0,drawdown=0)); session.commit()
    result=await TradingPipeline(session,Settings(**LEGACY),EventBus()).evaluate(MarketSnapshot("BTCUSDT",65000,2000,.4,.0001,"trending","BULLISH"))
    assert result["outcome"] == "TRADE"
    assert session.scalar(select(Order)).status == "FILLED"
    assert session.scalar(select(Position)).status == "OPEN"

