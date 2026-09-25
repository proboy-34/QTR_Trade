from collections.abc import AsyncIterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ZERO, decimal, money, price
from app.core.events import Event, EventBus, publish_persisted
from app.models import Position
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.positions import PositionManager


class PaperTradingLoop:
    """Deterministic, restart-safe coordinator for validated market snapshots."""

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session, self.settings, self.event_bus = session, settings, event_bus

    async def process(self, snapshot: MarketSnapshot) -> dict:
        decision = await TradingPipeline(self.session, self.settings, self.event_bus).evaluate(snapshot)
        closed: list[str] = []
        managed: list[str] = []
        positions = self.session.scalars(select(Position).where(
            Position.symbol == snapshot.symbol,
            Position.status.in_(["OPEN", "MANAGING"]),
        )).all()
        manager = PositionManager(self.session, self.settings.paper_fee_rate)
        for position in positions:
            mark = decimal(snapshot.price)
            manager.mark(position, snapshot.price)
            managed.append(position.id)
            reason = None
            if position.side == "BUY" and mark <= decimal(position.stop_loss):
                reason = "stop_loss"
            elif position.side == "BUY" and mark >= decimal(position.take_profit):
                reason = "take_profit"
            elif position.side == "SELL" and mark >= decimal(position.stop_loss):
                reason = "stop_loss"
            elif position.side == "SELL" and mark <= decimal(position.take_profit):
                reason = "take_profit"
            if reason:
                manager.close(position, snapshot.price, reason)
                closed.append(position.id)
                await publish_persisted(
                    self.session,
                    self.event_bus,
                    Event(
                        "PositionClosed",
                        {"position_id": position.id, "reason": reason},
                        source="position_management",
                    ),
                    "positions",
                )
        self.session.commit()
        return {"decision": decision, "managed_positions": managed, "closed_positions": closed}

    async def run(self, snapshots: AsyncIterable[MarketSnapshot]) -> list[dict]:
        results = []
        async for snapshot in snapshots:
            results.append(await self.process(snapshot))
        return results

    async def monitor_price(self, symbol: str, price_value: float) -> dict:
        """Mark paper positions from live ticks and enforce stored exits without re-deciding."""
        positions = self.session.scalars(select(Position).where(
            Position.symbol == symbol,
            Position.status.in_(["OPEN", "MANAGING"]),
        )).all()
        mark = price(price_value)
        closed: list[str] = []
        manager = PositionManager(self.session, self.settings.paper_fee_rate)
        for position in positions:
            direction = decimal(1 if position.side == "BUY" else -1)
            position.current_price = mark
            position.unrealized_pnl = money(
                (mark - decimal(position.entry_price)) * decimal(position.quantity) * direction
            )
            reason = None
            if position.side == "BUY" and mark <= decimal(position.stop_loss):
                reason = "stop_loss"
            elif position.side == "BUY" and mark >= decimal(position.take_profit):
                reason = "take_profit"
            elif position.side == "SELL" and mark >= decimal(position.stop_loss):
                reason = "stop_loss"
            elif position.side == "SELL" and mark <= decimal(position.take_profit):
                reason = "take_profit"
            if reason:
                manager.close(position, price_value, reason)
                closed.append(position.id)
                await publish_persisted(
                    self.session,
                    self.event_bus,
                    Event(
                        "PositionClosed",
                        {"position_id": position.id, "reason": reason},
                        source="live_paper_monitor",
                    ),
                    "positions",
                )
            elif position.unrealized_pnl == ZERO:
                position.unrealized_pnl = ZERO
        if positions:
            self.session.commit()
        return {"monitored_positions": len(positions), "closed_positions": closed}
