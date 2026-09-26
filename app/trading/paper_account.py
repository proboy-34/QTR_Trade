"""Paper account accounting derived from positions (never from hard-coded values).

For the spot paper venue (long only, no leverage):
  cash      = starting equity + realized P&L - all fees - cost of open positions
  equity    = cash + market value of open positions
  exposure  = market value / equity
  drawdown  = 1 - equity / peak equity (peak over all paper snapshots)
  daily P&L = equity - equity at the first snapshot of the current UTC day

Recomputing from positions after every fill, mark and close keeps the ledger restart-safe
and self-correcting; each recomputation is appended as a PortfolioSnapshot (the equity curve).
"""

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ONE, ZERO, decimal, money, rate
from app.core.time import TimeService
from app.models import Order, PaperAccountRecord, PortfolioSnapshot, Position

OPEN_STATES = ("OPENING", "OPEN", "MANAGING", "PARTIALLY_CLOSING", "CLOSING")


class PaperAccount:
    venue = "paper"

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session, self.settings = session, settings

    def record(self) -> PaperAccountRecord:
        """The persisted account; created once from STARTING_EQUITY (default $100,000)."""
        account = self.session.scalar(select(PaperAccountRecord).where(PaperAccountRecord.venue == self.venue))
        if account is None:
            account = PaperAccountRecord(venue=self.venue, currency="USD", initial_capital=money(decimal(self.settings.starting_equity)))
            self.session.add(account)
            self.session.flush()
        return account

    def initial_capital(self) -> Decimal:
        return decimal(self.record().initial_capital)

    def totals(self) -> dict[str, Decimal]:
        positions = self.session.scalars(select(Position).where(Position.venue == self.venue)).all()
        starting = self.initial_capital()
        realized = sum((decimal(item.realized_pnl) for item in positions), ZERO)
        fees = sum((decimal(item.fees) for item in positions), ZERO)
        open_positions = [item for item in positions if item.status in OPEN_STATES]
        cost = sum((decimal(item.entry_price) * decimal(item.quantity) for item in open_positions), ZERO)
        value = sum((decimal(item.current_price) * decimal(item.quantity) for item in open_positions), ZERO)
        cash = starting + realized - fees - cost
        equity = cash + value
        return {"starting_equity": starting, "realized_pnl": realized, "fees": fees, "open_cost": cost,
                "market_value": value, "unrealized_pnl": value - cost, "cash": cash, "equity": equity}

    def snapshot(self) -> PortfolioSnapshot:
        totals = self.totals()
        equity = totals["equity"]
        peak = self.session.scalar(select(func.max(PortfolioSnapshot.equity)).where(PortfolioSnapshot.venue == self.venue))
        peak_value = max(decimal(peak) if peak is not None else totals["starting_equity"], equity, totals["starting_equity"])
        day_start = TimeService.now().replace(hour=0, minute=0, second=0, microsecond=0)
        opening = self.session.scalar(select(PortfolioSnapshot.equity).where(
            PortfolioSnapshot.venue == self.venue, PortfolioSnapshot.captured_at < day_start,
        ).order_by(PortfolioSnapshot.captured_at.desc()))
        day_open = decimal(opening) if opening is not None else totals["starting_equity"]
        row = PortfolioSnapshot(
            venue=self.venue, equity=money(equity), available_balance=money(totals["cash"]),
            exposure=rate(totals["market_value"] / equity) if equity > ZERO else ONE,
            margin_used=money(totals["open_cost"]), daily_pnl=money(equity - day_open),
            drawdown=rate(ONE - equity / peak_value) if peak_value > ZERO else ZERO,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def summary(self) -> dict[str, Any]:
        totals = self.totals()
        closed = self.session.scalars(select(Position).where(Position.venue == self.venue, Position.status == "CLOSED")).all()
        net = [decimal(item.realized_pnl) - decimal(item.fees) for item in closed]
        slippage = (self.session.scalar(select(func.coalesce(func.sum(Order.slippage_cost), 0)).where(Order.exchange == self.venue)) or 0)
        exit_slippage = sum((decimal(item.slippage_cost) for item in closed), ZERO)
        curve = self.session.execute(select(PortfolioSnapshot.captured_at, PortfolioSnapshot.equity, PortfolioSnapshot.drawdown).where(
            PortfolioSnapshot.venue == self.venue).order_by(PortfolioSnapshot.captured_at.desc()).limit(500)).all()
        peak = max([decimal(row[1]) for row in curve] + [totals["starting_equity"]])
        max_dd = max([decimal(row[2]) for row in curve] + [ZERO])
        starting = totals["starting_equity"]
        configured = money(decimal(self.settings.starting_equity))
        return {
            "venue": "paper", "execution_mode": "PAPER", "market_data_source": "REAL_BINANCE", "currency": "USD",
            "initial_balance": str(money(starting)),
            "configured_starting_equity": str(configured),
            # The persisted initial capital is authoritative; a differing STARTING_EQUITY is ignored.
            "configured_starting_equity_ignored": configured != money(starting),
            "cash_available": str(money(totals["cash"])),
            "reserved_capital": str(money(totals["open_cost"])), "open_exposure": str(money(totals["market_value"])),
            "equity": str(money(totals["equity"])), "unrealized_pnl": str(money(totals["unrealized_pnl"])),
            "realized_pnl": str(money(totals["realized_pnl"])), "fees_paid": str(money(totals["fees"])),
            "slippage_cost": str(money(decimal(slippage) + exit_slippage)),
            "cumulative_return_pct": str(round((totals["equity"] / starting - ONE) * 100, 4)) if starting > ZERO else "0",
            "peak_equity": str(money(peak)), "max_drawdown": str(max_dd),
            "trades_closed": len(closed), "winning_trades": sum(1 for value in net if value > ZERO),
            "losing_trades": sum(1 for value in net if value <= ZERO),
            "open_positions": self.session.scalar(select(func.count()).select_from(Position).where(
                Position.venue == self.venue, Position.status.in_(OPEN_STATES))) or 0,
            "equity_curve": [{"at": TimeService.ensure_utc(at).isoformat(), "equity": str(equity)} for at, equity, _ in reversed(curve)],
        }
