from app.models import Order, Position
from app.trading.positions import PositionManager
from app.trading.reconciliation import ExchangeState, ReconciliationService


def open_position(session) -> Position:
    item = Position(
        strategy_version_id="version", symbol="BTCUSDT", side="BUY", quantity=1,
        entry_price=100, current_price=100, stop_loss=95, take_profit=110, status="OPEN",
    )
    session.add(item)
    session.commit()
    return item


def test_position_management_mark_break_even_and_close(session):
    position = open_position(session)
    manager = PositionManager(session)
    manager.mark(position, 105)
    assert position.status == "MANAGING" and position.unrealized_pnl == 5
    manager.move_to_break_even(position)
    assert position.stop_loss == position.entry_price
    manager.close(position, 108, "strategy exit")
    assert position.status == "CLOSED" and position.realized_pnl == 8
    assert position.closed_at is not None


def test_reconciliation_reports_without_correcting(session):
    position = open_position(session)
    order = Order(
        execution_plan_id="plan", position_id=position.id, exchange="paper", client_order_id="client",
        exchange_order_id="exchange-order", symbol="BTCUSDT", side="BUY", order_type="MARKET",
        quantity=1, fill_quantity=1, average_fill_price=100, fees=.1, status="FILLED",
    )
    session.add(order)
    session.commit()
    issues = ReconciliationService().reconcile(
        session, "paper", ExchangeState(
            orders={"exchange-order": {"status": "CANCELLED"}},
            positions={position.id: {"quantity": .5}},
        )
    )
    assert {issue.issue_type for issue in issues} == {"STATUS_MISMATCH", "QUANTITY_MISMATCH"}
    assert session.get(Order, order.id).status == "FILLED"
    assert session.get(Position, position.id).quantity == 1
