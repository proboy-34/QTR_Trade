from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.core.auth import LocalDemoAuthProvider, Role
from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.global_services.assets import AssetRegistryService
from app.global_services.connections import ConnectionManager
from app.global_services.historical import HistoricalBackfillService, SyntheticHistoricalProvider
from app.models import (
    Decision,
    ExecutionPlan,
    Fill,
    MarketCandle,
    MarketDataValidationFailure,
    Order,
    PortfolioSnapshot,
    Position,
    PositionEvent,
    SystemEvent,
    TradeIntent,
)
from app.trading.loop import PaperTradingLoop
from app.trading.paper_exchange import PaperExchange
from app.trading.pipeline import MarketSnapshot
from app.trading.positions import PositionManager
from app.trading.reconciliation import ExchangeState
from app.trading.recovery import RecoveryService


def intent(session, value: Decimal = Decimal("100")) -> TradeIntent:
    decision = Decision(
        symbol="BTCUSDT", outcome="TRADE", market_context={}, evaluations=[], confidence=1,
        reasoning=["test"], correlation_id="hardening-test",
    )
    session.add(decision)
    session.flush()
    item = TradeIntent(
        decision_id=decision.id, strategy_version_id="version", symbol="BTCUSDT", side="BUY",
        entry_price=value, stop_loss=None, take_profit=None, confidence=1,
    )
    session.add(item)
    session.flush()
    return item


def portfolio(session) -> None:
    session.add(PortfolioSnapshot(
        equity=100_000, available_balance=100_000, exposure=0, margin_used=0,
        daily_pnl=0, drawdown=0,
    ))
    session.commit()


@pytest.mark.asyncio
async def test_backfill_is_batched_checkpointed_and_duplicate_safe(session):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    service = HistoricalBackfillService(session)
    job = service.create("paper", "BTCUSDT", "1h", start, start + timedelta(hours=5), 2)
    await service.run(job, SyntheticHistoricalProvider())
    assert job.status == "COMPLETED"
    assert job.rows_written == 6
    assert session.scalar(select(func.count()).select_from(MarketCandle)) == 6

    duplicate = service.create("paper", "BTCUSDT", "1h", start, start + timedelta(hours=5), 2)
    await service.run(duplicate, SyntheticHistoricalProvider())
    assert duplicate.status == "COMPLETED" and duplicate.rows_written == 0
    assert session.scalar(select(func.count()).select_from(MarketCandle)) == 6


@pytest.mark.asyncio
async def test_backfill_retry_state_and_validation_failure_are_persisted(session):
    class Offline:
        name = "offline"

        async def fetch_batch(self, *args):
            raise httpx.ConnectError("offline")

    start = datetime(2024, 1, 1, tzinfo=UTC)
    service = HistoricalBackfillService(session, max_retries=1)
    job = service.create("paper", "BTCUSDT", "1h", start, start, 2)
    await service.run(job, Offline())
    assert job.status == "FAILED" and job.retry_count == 1

    class Invalid:
        name = "invalid"

        async def fetch_batch(self, symbol, timeframe, start, end, limit):
            return [{"timestamp": start, "open": -1, "high": 2, "low": 1, "close": 1, "volume": 1}]

    invalid = service.create("paper", "BTCUSDT", "1h", start, start, 2)
    await service.run(invalid, Invalid())
    assert invalid.status == "FAILED" and "INVALID_PRICE" in invalid.failure_reason
    assert session.scalar(select(func.count()).select_from(MarketDataValidationFailure)) == 1


@pytest.mark.asyncio
async def test_backfill_resumes_from_last_committed_batch(session):
    start = datetime(2024, 1, 1, tzinfo=UTC)

    class InterruptAfterOne:
        name = "interrupt"

        def __init__(self):
            self.calls = 0

        async def fetch_batch(self, symbol, timeframe, batch_start, end, limit):
            self.calls += 1
            if self.calls > 1:
                raise httpx.ConnectError("interrupted")
            return await SyntheticHistoricalProvider().fetch_batch(
                symbol, timeframe, batch_start, end, limit
            )

    service = HistoricalBackfillService(session, max_retries=1)
    job = service.create("paper", "BTCUSDT", "1h", start, start + timedelta(hours=4), 2)
    await service.run(job, InterruptAfterOne())
    checkpoint = job.next_start_at
    assert job.status == "FAILED" and job.rows_written == 2
    await service.run(job, SyntheticHistoricalProvider())
    assert job.status == "COMPLETED" and job.rows_written == 5
    assert job.next_start_at > checkpoint


def _plan(session, *, order_type="MARKET", limit_price=None, quantity=Decimal("2")):
    trade_intent = intent(session)
    plan = ExecutionPlan(
        trade_intent_id=trade_intent.id, exchange="paper", symbol="BTCUSDT", side="BUY",
        quantity=quantity, order_type=order_type, status="approved", risk_amount=10,
        stop_loss=95, take_profit=110, leverage=1, limit_price=limit_price,
    )
    session.add(plan)
    session.flush()
    return trade_intent, plan


@pytest.mark.asyncio
async def test_limit_monitor_partial_fill_and_cancel_lifecycle(session):
    portfolio(session)
    trade_intent, plan = _plan(session, order_type="LIMIT", limit_price=99)
    exchange = PaperExchange(session, Settings(paper_partial_fill_ratio=Decimal("0.5")), EventBus())
    submitted = await exchange.submit(plan, trade_intent, 100, "limit")
    order = session.get(Order, submitted["order_id"])
    assert order.status == "NEW"
    first_fill = await exchange.monitor(order, plan, trade_intent, 98, "limit")
    assert first_fill["position_id"] and order.status == "PARTIALLY_FILLED"
    await exchange.monitor(order, plan, trade_intent, 98, "limit")
    assert order.status == "FILLED" and order.fill_quantity == Decimal("2")
    assert session.scalar(select(func.count()).select_from(Fill)) == 2

    other_intent, other_plan = _plan(session, order_type="LIMIT", limit_price=90)
    other = await exchange.submit(other_plan, other_intent, 100, "cancel")
    other_order = session.get(Order, other["order_id"])
    await exchange.cancel(other_order, "cancel")
    assert other_order.status == "CANCELLED"


@pytest.mark.asyncio
async def test_paper_exchange_rejects_insufficient_balance(session):
    portfolio(session)
    trade_intent, plan = _plan(session, quantity=Decimal("2000"))
    result = await PaperExchange(session, Settings(), EventBus()).submit(
        plan, trade_intent, 100, "insufficient"
    )
    order = session.get(Order, result["order_id"])
    assert result["rejected"] and "INSUFFICIENT_BALANCE" in result["errors"]
    assert order.status == "REJECTED"
    assert session.scalar(select(func.count()).select_from(Fill)) == 0


def test_decimal_position_accounting_and_partial_close_transitions(session):
    portfolio(session)
    position = Position(
        strategy_version_id="version", symbol="BTCUSDT", side="BUY",
        quantity=Decimal("2.000000000000"), entry_price=Decimal("100.100000000000"),
        current_price=Decimal("100.100000000000"), stop_loss=90, take_profit=120,
        status="OPEN",
    )
    session.add(position)
    session.commit()
    manager = PositionManager(session, fee_rate=0)
    manager.partial_close(position, Decimal("0.75"), Decimal("101.10"), "scale out")
    assert position.quantity == Decimal("1.250000000000")
    assert position.realized_pnl == Decimal("0.7500000000")
    transitions = session.scalars(select(PositionEvent).where(
        PositionEvent.position_id == position.id
    ).order_by(PositionEvent.occurred_at)).all()
    assert [(item.from_status, item.to_status) for item in transitions] == [
        ("OPEN", "PARTIALLY_CLOSING"), ("PARTIALLY_CLOSING", "MANAGING")
    ]
    manager.close(position, Decimal("102.10"), "target")
    assert position.status == "CLOSED"
    assert position.realized_pnl == Decimal("3.2500000000")
    assert position.exit_reason == "target"
    with pytest.raises(ValueError, match="not closable"):
        manager.close(position, 103, "impossible second close")


def test_asset_registry_enforces_precision_and_trading_status(session):
    registry = AssetRegistryService(session)
    assert registry.validate_order("paper", "BTCUSDT", Decimal("0.1"), Decimal("100")).valid
    invalid = registry.validate_order("paper", "BTCUSDT", Decimal("0.100001"), Decimal("100.01"))
    assert {"INVALID_QUANTITY_STEP", "INVALID_PRICE_TICK"}.issubset(invalid.errors)


@pytest.mark.asyncio
async def test_persisted_events_are_idempotent(session):
    event = Event("Once", {"value": 1}, event_id="same-event")
    bus = EventBus()
    await publish_persisted(session, bus, event, "test")
    await publish_persisted(session, bus, event, "test")
    session.commit()
    assert session.scalar(select(func.count()).select_from(SystemEvent)) == 1
    assert bus.published == 1


@pytest.mark.asyncio
async def test_connection_manager_tracks_failures_and_reconnects():
    class Probe:
        def __init__(self):
            self.healthy = False

        async def ping(self):
            return self.healthy

    probe = Probe()
    manager = ConnectionManager(EventBus(), max_attempts=1)
    failed = await manager.connect("paper", probe)
    assert failed.status == "ERROR" and failed.reconnect_attempts == 1
    probe.healthy = True
    connected = await manager.connect("paper", probe, authenticated=True, websocket_healthy=True)
    assert connected.status == "CONNECTED" and connected.authenticated and connected.websocket_healthy


@pytest.mark.asyncio
async def test_local_auth_is_explicit_operator_foundation():
    principal = await LocalDemoAuthProvider().authenticate(None)
    assert principal.provider == "local_demo"
    assert principal.can(Role.OPERATOR) and principal.can(Role.AUDITOR)


def test_recovery_reports_pending_and_open_state_without_corrective_order(session):
    position = Position(
        strategy_version_id="version", symbol="BTCUSDT", side="BUY", quantity=1,
        entry_price=100, current_price=100, stop_loss=95, take_profit=110, status="OPEN",
    )
    session.add(position)
    session.flush()
    order = Order(
        execution_plan_id="plan", position_id=position.id, exchange="paper",
        client_order_id="pending-client", exchange_order_id="pending-exchange",
        symbol="BTCUSDT", side="BUY", order_type="LIMIT", quantity=2,
        fill_quantity=1, fees=0, status="PARTIALLY_FILLED", raw_response={},
    )
    session.add(order)
    session.commit()
    result = RecoveryService().recover(session, "paper", ExchangeState(
        orders={"pending-exchange": {"status": "PARTIALLY_FILLED"}},
        positions={position.id: {"quantity": 1}},
    ))
    assert result == {
        "pending_orders": 1, "open_positions": 1, "issues": 0,
        "manual_intervention_required": False, "corrective_orders": 0,
    }
    assert session.get(Order, order.id).fill_quantity == 1


@pytest.mark.asyncio
async def test_paper_loop_remains_stable_across_repeated_wait_and_ignore(session):
    loop = PaperTradingLoop(session, Settings(), EventBus())
    waits = [await loop.process(MarketSnapshot(
        "ETHUSDT", 3_000, 100, .2, 0, "sideways", "BEARISH"
    )) for _ in range(5)]
    ignored = await loop.process(MarketSnapshot(
        "ETHUSDT", -1, 100, .2, 0, "sideways", "BEARISH"
    ))
    assert all(item["decision"]["outcome"] == "WAIT" for item in waits)
    assert ignored["decision"]["outcome"] == "IGNORE"
    assert session.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.asyncio
async def test_paper_loop_marks_and_closes_existing_position_at_target(session):
    portfolio(session)
    position = Position(
        strategy_version_id="version", symbol="BTCUSDT", side="BUY", quantity=1,
        entry_price=100, current_price=100, stop_loss=95, take_profit=110, status="OPEN",
    )
    session.add(position)
    session.commit()
    # Frictionless configuration: exit fills exactly at the observed mark.
    frictionless = Settings(paper_slippage_rate=0, paper_spread_bps=0)
    result = await PaperTradingLoop(session, frictionless, EventBus()).process(MarketSnapshot(
        "BTCUSDT", 111, 100, .2, 0, "sideways", "NEUTRAL"
    ))
    assert result["closed_positions"] == [position.id]
    assert position.status == "CLOSED" and position.exit_reason == "take_profit"
    assert position.realized_pnl == Decimal("11.0000000000")


@pytest.mark.asyncio
async def test_paper_exit_applies_slippage_and_half_spread_against_the_position(session):
    portfolio(session)
    position = Position(
        strategy_version_id="version", symbol="BTCUSDT", side="BUY", quantity=1,
        entry_price=100, current_price=100, stop_loss=95, take_profit=110, status="OPEN",
    )
    session.add(position)
    session.commit()
    settings = Settings(paper_slippage_rate=0.001, paper_spread_bps=20)
    await PaperTradingLoop(session, settings, EventBus()).monitor_price("BTCUSDT", 90)
    # Gap through the 95 stop: fill at the observed 90 minus 0.1% slippage and 0.1% half-spread.
    assert position.status == "CLOSED" and position.exit_reason == "stop_loss"
    assert position.current_price == Decimal("89.820000000000")
    assert position.realized_pnl == Decimal("-10.1800000000")
