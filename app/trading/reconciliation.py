from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, Position, ReconciliationIssue


@dataclass
class ExchangeState:
    orders: dict[str, dict]
    positions: dict[str, dict]


class ReconciliationService:
    """Reports mismatches only; never submits corrective orders."""

    def reconcile(self, session: Session, exchange: str, state: ExchangeState) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        orders = session.scalars(select(Order).where(Order.exchange == exchange)).all()
        for order in orders:
            remote = state.orders.get(order.exchange_order_id)
            if not remote:
                issues.append(self._issue(exchange, "order", order.id, "MISSING_EXCHANGE_ORDER", {"status": order.status}, {}))
            elif remote.get("status") != order.status:
                issues.append(self._issue(exchange, "order", order.id, "STATUS_MISMATCH", {"status": order.status}, remote))
        positions = session.scalars(select(Position).where(Position.status != "CLOSED")).all()
        for position in positions:
            remote = state.positions.get(position.id)
            if not remote:
                issues.append(self._issue(exchange, "position", position.id, "MISSING_EXCHANGE_POSITION", {"quantity": position.quantity}, {}))
            elif abs(Decimal(str(remote.get("quantity", 0))) - position.quantity) > Decimal("0.00000001"):
                issues.append(self._issue(exchange, "position", position.id, "QUANTITY_MISMATCH", {"quantity": position.quantity}, remote))
        session.add_all(issues)
        session.commit()
        return issues

    @staticmethod
    def _issue(exchange: str, entity_type: str, entity_id: str, issue_type: str, internal: dict, remote: dict) -> ReconciliationIssue:
        return ReconciliationIssue(exchange=exchange, entity_type=entity_type, entity_id=entity_id, issue_type=issue_type, internal_state=internal, exchange_state=remote)
