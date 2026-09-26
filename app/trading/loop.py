from collections.abc import AsyncIterable
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ONE, ZERO, decimal, money, price
from app.core.events import Event, EventBus, publish_persisted
from app.global_services.quotes import QuoteProvider
from app.memory.trade_memory import TradeMemoryService
from app.models import Position, PositionEvent, StrategyVersion
from app.research.dsl import spec_from_version
from app.trading.pipeline import MarketSnapshot, TradingPipeline, latest_signals
from app.trading.positions import PositionManager, track_extremes


def exit_reason(position: Position, mark: Decimal) -> str | None:
    if position.side == "BUY" and mark <= decimal(position.stop_loss):
        return "stop_loss"
    if position.side == "BUY" and mark >= decimal(position.take_profit):
        return "take_profit"
    if position.side == "SELL" and mark >= decimal(position.stop_loss):
        return "stop_loss"
    if position.side == "SELL" and mark <= decimal(position.take_profit):
        return "take_profit"
    return None


class PaperTradingLoop:
    """Deterministic, restart-safe coordinator for validated market snapshots."""

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus, quote_provider: QuoteProvider | None = None) -> None:
        self.session, self.settings, self.event_bus = session, settings, event_bus
        self.quote_provider = quote_provider

    def exit_fill(self, position: Position, mark: Decimal) -> Decimal:
        """Protective exits are market orders: slippage and half the spread apply against us.

        A gap through the stop fills at the (worse) observed price, never at the stop level.
        """
        adverse = decimal(self.settings.paper_slippage_rate) + decimal(self.settings.paper_spread_bps) / 20_000
        return price(mark * (ONE - adverse if position.side == "BUY" else ONE + adverse))

    async def _close(self, position: Position, mark: Decimal, reason: str, source: str) -> None:
        exit_price = self.exit_fill(position, mark)
        await self.close_at(position, exit_price, reason, source, money(abs(mark - exit_price) * decimal(position.quantity)))

    async def close_at(self, position: Position, exit_price: Decimal, reason: str, source: str,
                       slippage_cost: Decimal | None = None) -> None:
        """Close at an already-determined paper exit price (testnet positions exit on the testnet)."""
        manager = PositionManager(self.session, self.settings.paper_fee_rate)
        mark = exit_price
        if position.venue == "testnet":
            from app.execution.binance_testnet import BinanceTestnetExchange

            try:
                filled = await BinanceTestnetExchange(self.session, self.settings, self.event_bus).close(position, reason)
            except Exception as exc:  # a failed exit stays visible; the next price update retries it
                self.session.add(PositionEvent(position_id=position.id, event_type="EXIT_FAILED", from_status=position.status,
                                               to_status=position.status, price=mark, quantity=position.quantity,
                                               reason=f"{reason}: {type(exc).__name__}"))
                self.session.commit()
                return
            exit_price = filled or exit_price
        manager.close(position, exit_price, reason, slippage_cost)
        trade = TradeMemoryService(self.session).record(position)
        await publish_persisted(
            self.session, self.event_bus,
            Event("PositionClosed", {"position_id": position.id, "reason": reason}, source=source),
            "positions",
        )
        await publish_persisted(self.session, self.event_bus, Event("TRADE_CLOSED", {
            "position_id": position.id, "trade_memory_id": trade.id if trade else None, "symbol": position.symbol,
            "reason": reason, "net_pnl": str(trade.net_pnl) if trade else None,
        }, source=source), "positions")

    def _strategy_exit(self, position: Position, snapshot: MarketSnapshot) -> str | None:
        """Apply the strategy's own declarative exit rule on the closed candle (as in research)."""
        version = self.session.get(StrategyVersion, position.strategy_version_id)
        spec = spec_from_version(version, version.strategy) if version else None
        if spec is None or not spec.exit or snapshot.timeframe not in spec.timeframes:
            return None
        signals, _ = latest_signals(self.session, spec, snapshot)
        return "strategy_exit" if signals and signals["exit"] else None

    async def process(self, snapshot: MarketSnapshot) -> dict:
        decision = await TradingPipeline(self.session, self.settings, self.event_bus, self.quote_provider).evaluate(snapshot)
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
            reason = exit_reason(position, mark) or self._strategy_exit(position, snapshot)
            if reason:
                await self._close(position, mark, reason, "position_management")
                closed.append(position.id)
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
        for position in positions:
            direction = decimal(1 if position.side == "BUY" else -1)
            position.current_price = mark
            track_extremes(position, mark)
            position.unrealized_pnl = money(
                (mark - decimal(position.entry_price)) * decimal(position.quantity) * direction
            )
            reason = exit_reason(position, mark)
            if reason:
                await self._close(position, mark, reason, "live_paper_monitor")
                closed.append(position.id)
            elif position.unrealized_pnl == ZERO:
                position.unrealized_pnl = ZERO
        if positions:
            self.session.commit()
        return {"monitored_positions": len(positions), "closed_positions": closed}
