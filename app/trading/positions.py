from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.decimal_math import ZERO, decimal, money, price, quantity, rate
from app.core.time import TimeService
from app.models import PortfolioSnapshot, Position, PositionEvent
from app.trading.accounts import latest_portfolio


def track_extremes(position: Position, mark_price) -> None:
    """Maintain the high/low seen while open (input to MAE/MFE in trade memory)."""
    if position.highest_price is None or mark_price > decimal(position.highest_price):
        position.highest_price = mark_price
    if position.lowest_price is None or mark_price < decimal(position.lowest_price):
        position.lowest_price = mark_price


class PositionManager:
    def __init__(self, session: Session, fee_rate: float = 0.0004) -> None:
        self.session = session
        self.fee_rate = decimal(fee_rate)

    def _portfolio_after_close(
        self, position: Position, close_quantity, exit_price, realized, fee
    ) -> None:
        latest = latest_portfolio(self.session, position.venue or "paper")
        if not latest:
            return
        equity_before = decimal(latest.equity)
        entry_notional = money(decimal(position.entry_price) * close_quantity)
        released = money(exit_price * close_quantity)
        exposure_reduction = entry_notional / max(equity_before, decimal(1))
        self.session.add(PortfolioSnapshot(
            venue=position.venue or "paper",
            equity=money(equity_before + realized - fee),
            available_balance=money(decimal(latest.available_balance) + released - fee),
            exposure=rate(max(ZERO, decimal(latest.exposure) - exposure_reduction)),
            margin_used=money(max(ZERO, decimal(latest.margin_used) - entry_notional)),
            daily_pnl=money(decimal(latest.daily_pnl) + realized - fee),
            drawdown=latest.drawdown,
        ))

    def mark(self, position: Position, price_value: float | Decimal) -> Position:
        if position.status not in {"OPEN", "MANAGING"}:
            raise ValueError("Only open positions can be managed")
        previous = position.status
        position.status = "MANAGING"
        mark_price = price(price_value)
        position.current_price = mark_price
        track_extremes(position, mark_price)
        direction = 1 if position.side == "BUY" else -1
        position.unrealized_pnl = money(
            (mark_price - decimal(position.entry_price)) * decimal(position.quantity) * direction
        )
        self.session.add(PositionEvent(
            position_id=position.id, event_type="MARKED", from_status=previous, to_status="MANAGING",
            price=mark_price, quantity=position.quantity, reason="mark to market",
        ))
        self.session.commit()
        return position

    def move_to_break_even(self, position: Position) -> Position:
        previous_stop = position.stop_loss
        position.stop_loss = position.entry_price
        self.session.add(PositionEvent(
            position_id=position.id, event_type="BREAK_EVEN", from_status=position.status,
            to_status="MANAGING", price=position.entry_price, quantity=position.quantity,
            reason=f"stop moved from {previous_stop}",
        ))
        position.status = "MANAGING"
        self.session.commit()
        return position

    def partial_close(
        self, position: Position, close_quantity: float | Decimal, price_value: float | Decimal, reason: str,
    ) -> Position:
        if position.status not in {"OPEN", "MANAGING"}:
            raise ValueError("Position is not closable")
        amount = quantity(close_quantity)
        if amount <= ZERO or amount >= decimal(position.quantity):
            raise ValueError("Partial close quantity must be positive and below open quantity")
        exit_price = price(price_value)
        direction = 1 if position.side == "BUY" else -1
        realized = money((exit_price - decimal(position.entry_price)) * amount * direction)
        fee = money(exit_price * amount * self.fee_rate)
        previous = position.status
        position.quantity = quantity(decimal(position.quantity) - amount)
        position.realized_pnl = money(decimal(position.realized_pnl) + realized)
        position.fees = money(decimal(position.fees) + fee)
        position.status = "PARTIALLY_CLOSING"
        self.session.add(PositionEvent(
            position_id=position.id, event_type="PARTIAL_CLOSE", from_status=previous,
            to_status="PARTIALLY_CLOSING", price=exit_price, quantity=amount, reason=reason,
        ))
        position.status = "MANAGING"
        self.session.add(PositionEvent(
            position_id=position.id, event_type="MANAGING", from_status="PARTIALLY_CLOSING",
            to_status="MANAGING", price=exit_price, quantity=position.quantity, reason="partial close filled",
        ))
        self._portfolio_after_close(position, amount, exit_price, realized, fee)
        self.session.commit()
        return position

    def close(self, position: Position, price_value: float | Decimal, reason: str) -> Position:
        if position.status not in {"OPEN", "MANAGING"}:
            raise ValueError("Position is not closable")
        previous = position.status
        direction = 1 if position.side == "BUY" else -1
        position.status = "CLOSED"
        exit_price = price(price_value)
        position.current_price = exit_price
        position.realized_pnl = money(
            decimal(position.realized_pnl)
            + (exit_price - decimal(position.entry_price)) * decimal(position.quantity) * direction
        )
        fee = money(exit_price * decimal(position.quantity) * self.fee_rate)
        incremental_realized = money(
            (exit_price - decimal(position.entry_price)) * decimal(position.quantity) * direction
        )
        position.fees = money(decimal(position.fees) + fee)
        position.unrealized_pnl = ZERO
        position.exit_reason = reason
        position.closed_at = TimeService.now()
        self.session.add(PositionEvent(
            position_id=position.id, event_type="CLOSED", from_status=previous, to_status="CLOSED",
            price=exit_price, quantity=position.quantity, reason=reason,
        ))
        self._portfolio_after_close(
            position, decimal(position.quantity), exit_price, incremental_realized, fee
        )
        self.session.commit()
        return position
