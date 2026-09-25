from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, Position, SystemEvent
from app.trading.reconciliation import ExchangeState, ReconciliationService


class RecoveryService:
    """Restores and reports incomplete state without creating any order."""

    def recover(self, session: Session, exchange: str, state: ExchangeState) -> dict:
        pending = session.scalars(select(Order).where(
            Order.exchange == exchange,
            Order.status.in_(["SUBMITTED", "NEW", "PARTIALLY_FILLED"]),
        )).all()
        open_positions = session.scalars(select(Position).where(
            Position.status.in_(["OPENING", "OPEN", "MANAGING", "PARTIALLY_CLOSING", "CLOSING"])
        )).all()
        issues = ReconciliationService().reconcile(session, exchange, state)
        manual = bool(issues)
        session.add(SystemEvent(
            type="RecoveryCompleted",
            component="recovery",
            severity="WARNING" if manual else "INFO",
            message="Recovery requires manual review" if manual else "Recovery completed",
            payload={
                "pending_order_ids": [item.id for item in pending],
                "open_position_ids": [item.id for item in open_positions],
                "issue_ids": [item.id for item in issues],
                "corrective_orders": 0,
            },
            correlation_id="startup-recovery",
        ))
        session.commit()
        return {
            "pending_orders": len(pending),
            "open_positions": len(open_positions),
            "issues": len(issues),
            "manual_intervention_required": manual,
            "corrective_orders": 0,
        }
