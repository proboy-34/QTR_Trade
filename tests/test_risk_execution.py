import hashlib

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.events import EventBus
from app.models import (
    AssetInstrument,
    Decision,
    ExecutionPlan,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    RiskEvent,
    Strategy,
    StrategyVersion,
    TradeIntent,
)
from app.trading.paper_exchange import PaperExchange
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.risk import RiskEngine


def intent(session, price: float = 100) -> TradeIntent:
    decision = Decision(
        symbol="BTCUSDT", outcome="TRADE", market_context={}, evaluations=[], confidence=1,
        reasoning=["test"], correlation_id="risk-test",
    )
    session.add(decision)
    session.flush()
    item = TradeIntent(
        decision_id=decision.id, strategy_version_id="version", symbol="BTCUSDT", side="BUY",
        entry_price=price, stop_loss=None, take_profit=None, confidence=1,
    )
    session.add(item)
    session.flush()
    return item


def portfolio(session, *, equity=100_000, available=100_000, exposure=0, daily_pnl=0, drawdown=0):
    session.add(PortfolioSnapshot(
        equity=equity, available_balance=available, exposure=exposure, margin_used=0,
        daily_pnl=daily_pnl, drawdown=drawdown,
    ))
    session.commit()


def test_risk_rejects_zero_balance(session):
    portfolio(session, equity=0, available=0)
    result = RiskEngine(session, Settings()).assess(intent(session), 100, 0.2)
    assert not result.approved and "ZERO_OR_NEGATIVE_CAPITAL" in result.reason_codes


def test_risk_rejects_daily_loss_and_drawdown(session):
    portfolio(session, daily_pnl=-4_000, drawdown=0.2)
    result = RiskEngine(session, Settings()).assess(intent(session), 100, 0.2)
    assert {"DAILY_LOSS_LIMIT", "MAX_DRAWDOWN"}.issubset(result.reason_codes)


def test_risk_rejects_max_exposure(session):
    portfolio(session, exposure=0.5)
    result = RiskEngine(session, Settings()).assess(intent(session), 100, 0.2)
    assert not result.approved and "MAX_EXPOSURE" in result.reason_codes


def test_risk_rejects_max_positions_and_extreme_volatility(session):
    portfolio(session)
    for index in range(2):
        session.add(Position(
            strategy_version_id=f"version-{index}", symbol=f"ASSET{index}USDT", side="BUY",
            quantity=1, entry_price=10, current_price=10, stop_loss=9, take_profit=12,
            status="OPEN",
        ))
    session.commit()
    result = RiskEngine(session, Settings(max_open_positions=2)).assess(intent(session), 100, 20)
    assert {"MAX_POSITIONS", "INVALID_MARKET_INPUT"}.issubset(result.reason_codes)


def test_risk_rejects_asset_minimum_and_insufficient_margin(session):
    portfolio(session, available=10)
    instrument = session.scalar(select(AssetInstrument).where(AssetInstrument.exchange == "paper"))
    instrument.step_size = 1
    instrument.min_quantity = 1000
    instrument.min_notional = 1_000_000
    session.commit()
    result = RiskEngine(session, Settings()).assess(intent(session), 100, 0.2)
    assert not result.approved
    assert "INVALID_OR_BELOW_MIN_QUANTITY" in result.reason_codes
    assert "INSUFFICIENT_MARGIN" in result.reason_codes


@pytest.mark.asyncio
async def test_valid_opportunity_rejected_by_risk_creates_no_order_or_position(session):
    strategy = Strategy(name="Risk rejection", symbol="BTCUSDT", timeframe="1h", status="active")
    session.add(strategy)
    session.flush()
    session.add(StrategyVersion(
        strategy_id=strategy.id, version=1, parameters={}, entry_rules=[{"rule": "bullish"}],
        exit_rules=[{"rule": "reverse"}], filters={"regime": ["trending"]},
        risk_assumptions={}, documentation="risk rejection test strategy",
        content_hash=hashlib.sha256(b"risk").hexdigest(),
    ))
    portfolio(session, exposure=0.5)
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(
        MarketSnapshot("BTCUSDT", 65_000, 2_000, .4, 0, "trending", "BULLISH")
    )
    assert result["outcome"] == "TRADE" and result["risk_outcome"] == "REJECTED"
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    assert session.scalar(select(func.count()).select_from(Position)) == 0
    assert session.scalar(select(RiskEvent)).outcome == "REJECTED"


@pytest.mark.asyncio
async def test_paper_order_submission_is_idempotent(session):
    portfolio(session)
    trade_intent = intent(session)
    plan = ExecutionPlan(
        trade_intent_id=trade_intent.id, exchange="paper", symbol="BTCUSDT", side="BUY",
        quantity=2, order_type="MARKET", status="approved", risk_amount=10,
        stop_loss=95, take_profit=110, leverage=1,
    )
    session.add(plan)
    session.flush()
    exchange = PaperExchange(session, Settings(), EventBus())
    first = await exchange.submit(plan, trade_intent, 100, "idem")
    second = await exchange.submit(plan, trade_intent, 100, "idem")
    session.commit()
    assert first["order_id"] == second["order_id"] and second["idempotent"]
    assert session.scalar(select(func.count()).select_from(Order)) == 1
    assert session.scalar(select(func.count()).select_from(Fill)) == 1
    assert session.scalar(select(func.count()).select_from(Position)) == 1


@pytest.mark.asyncio
async def test_configurable_partial_fill(session):
    portfolio(session)
    trade_intent = intent(session)
    plan = ExecutionPlan(
        trade_intent_id=trade_intent.id, exchange="paper", symbol="BTCUSDT", side="BUY",
        quantity=2, order_type="MARKET", status="approved", risk_amount=10,
        stop_loss=95, take_profit=110, leverage=1,
    )
    session.add(plan)
    session.flush()
    settings = Settings(paper_partial_fill_ratio=.5)
    result = await PaperExchange(session, settings, EventBus()).submit(plan, trade_intent, 100, "partial")
    order = session.get(Order, result["order_id"])
    assert order.status == "PARTIALLY_FILLED" and order.fill_quantity == 1
