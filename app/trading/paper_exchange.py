import asyncio
from decimal import ROUND_CEILING, ROUND_FLOOR
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ONE, ZERO, decimal, money, price, quantity
from app.core.errors import SafetyError
from app.core.events import Event, EventBus, publish_persisted
from app.global_services.assets import AssetRegistryService
from app.global_services.quotes import Quote
from app.models import (
    ExecutionPlan,
    ExecutionReport,
    Fill,
    Order,
    Position,
    PositionEvent,
    TradeIntent,
)
from app.trading.accounts import latest_portfolio
from app.trading.paper_account import PaperAccount
from app.trading.safety import SafetyService


class PaperExchange:
    """Paper venue: SIMULATED execution against real market quotes, idempotent client IDs.

    With a real quote a BUY pays the real ask and a SELL receives the real bid, plus the
    configured slippage. Without a quote (deterministic fixtures, demo) the reference price
    gets slippage plus half the configured spread, and the order is labelled accordingly.
    Orders always carry execution_mode="PAPER": they are never presented as exchange fills.
    """

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus,
                 quote: Quote | None = None, instrument_exchange: str | None = None) -> None:
        self.session, self.settings, self.event_bus = session, settings, event_bus
        self.quote = quote
        self.instrument_exchange = instrument_exchange

    def _instrument(self, symbol: str):
        registry = AssetRegistryService(self.session)
        if self.instrument_exchange:
            found = registry.instrument(self.instrument_exchange, symbol)
            if found:
                return found
        return registry.instrument("paper", symbol)

    def _source(self) -> str:
        if self.quote is not None:
            return self.quote.source
        return "DEMO_SYNTHETIC" if self.settings.demo_mode else "REFERENCE_PRICE"

    async def submit(
        self, plan: ExecutionPlan, intent: TradeIntent, market_price: float, correlation_id: str
    ) -> dict:
        started = perf_counter()
        if self.settings.trading_mode != "paper" or plan.exchange != "paper":
            raise SafetyError("No authenticated live execution adapter is installed")
        if plan.order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise SafetyError("Unsupported paper order type")
        client_order_id = f"qtr-{plan.id}"
        existing = self.session.scalar(select(Order).where(Order.client_order_id == client_order_id))
        if existing:
            return {"order_id": existing.id, "position_id": existing.position_id, "idempotent": True}
        # Defense in depth: Risk already rejects under a kill switch; the venue refuses too.
        halted = SafetyService(self.session).blocks_new_orders(None, plan.symbol)
        if halted:
            raise SafetyError(f"New paper orders are halted: {', '.join(halted)}")
        validation_price = decimal(plan.limit_price or plan.stop_price or market_price)
        if plan.order_type == "MARKET":
            # A market order has no price of its own; it fills on the venue's tick grid, so the
            # observed reference price is aligned to that grid before instrument validation.
            reference = self._instrument(plan.symbol)
            if reference and decimal(reference.tick_size) > ZERO:
                tick = decimal(reference.tick_size)
                validation_price = (validation_price / tick).to_integral_value() * tick
        instrument = self._instrument(plan.symbol)
        validation = AssetRegistryService(self.session).validate_order(
            instrument.exchange if instrument else "paper", plan.symbol, decimal(plan.quantity), validation_price
        )
        validation_errors = list(validation.errors)
        latest = latest_portfolio(self.session, "paper")
        available = decimal(latest.available_balance) if latest else decimal(self.settings.starting_equity)
        leverage = decimal(plan.leverage)
        if leverage <= ZERO:
            validation_errors.append("INVALID_LEVERAGE")
        else:
            estimated_fee = money(
                decimal(plan.quantity) * validation_price * decimal(self.settings.paper_fee_rate)
            )
            required = money(decimal(plan.quantity) * validation_price / leverage + estimated_fee)
            if required > available:
                validation_errors.append("INSUFFICIENT_BALANCE")
        if validation_errors:
            order = Order(
                execution_plan_id=plan.id, exchange="paper", client_order_id=client_order_id,
                exchange_order_id=f"paper-{uuid4().hex[:12]}", symbol=plan.symbol, side=plan.side,
                order_type=plan.order_type, quantity=plan.quantity, fill_quantity=ZERO,
                fees=ZERO, status="REJECTED", raw_response={"errors": validation_errors},
                **self._provenance(market_price),
            )
            self.session.add(order)
            self.session.flush()
            self._report(order, started)
            return {"order_id": order.id, "position_id": None, "rejected": True, "errors": validation_errors}
        order = Order(
            execution_plan_id=plan.id,
            exchange="paper",
            client_order_id=client_order_id,
            exchange_order_id=f"paper-{uuid4().hex[:12]}",
            symbol=plan.symbol,
            side=plan.side,
            order_type=plan.order_type,
            quantity=plan.quantity,
            fill_quantity=ZERO,
            fees=ZERO,
            status="SUBMITTED",
            raw_response={"simulated": True, "note": "paper order: simulated fill, not an exchange fill"},
            **self._provenance(market_price),
        )
        self.session.add(order)
        self.session.flush()
        await publish_persisted(
            self.session, self.event_bus, Event("OrderSubmitted", {"order_id": order.id}, correlation_id), "execution"
        )
        if self.settings.paper_latency_ms:
            await asyncio.sleep(self.settings.paper_latency_ms / 1000)
        should_fill = self.settings.paper_immediate_fill and self._should_fill(plan, market_price)
        if not should_fill:
            order.status = "NEW"
            self._report(order, started)
            return {"order_id": order.id, "position_id": None, "idempotent": False}
        return await self._fill(order, plan, intent, market_price, correlation_id, started)

    def _provenance(self, market_price: float) -> dict:
        return {"execution_mode": "PAPER", "market_data_source": self._source(),
                "reference_price": price(market_price),
                "bid": self.quote.bid if self.quote else None, "ask": self.quote.ask if self.quote else None,
                "quote_at": self.quote.observed_at if self.quote else None}

    def fill_price(self, side: str, market_price: float) -> tuple:
        """(fill price, touch price) for a market order on this venue."""
        slip = decimal(self.settings.paper_slippage_rate)
        if self.quote is not None:
            touch = self.quote.ask if side == "BUY" else self.quote.bid
            return price(touch * (ONE + slip if side == "BUY" else ONE - slip)), touch
        adverse = slip + decimal(self.settings.paper_spread_bps) / 20_000
        reference = decimal(market_price)
        return price(reference * (ONE + adverse if side == "BUY" else ONE - adverse)), reference

    def _should_fill(self, plan: ExecutionPlan, market_price: float) -> bool:
        market = decimal(market_price)
        return (
            plan.order_type == "MARKET"
            or (plan.order_type == "LIMIT" and plan.side == "BUY" and market <= decimal(plan.limit_price or ZERO))
            or (plan.order_type == "LIMIT" and plan.side == "SELL" and market >= decimal(plan.limit_price or decimal("Infinity")))
            or (plan.order_type == "STOP" and plan.side == "BUY" and market >= decimal(plan.stop_price or decimal("Infinity")))
            or (plan.order_type == "STOP" and plan.side == "SELL" and market <= decimal(plan.stop_price or ZERO))
        )

    async def monitor(
        self, order: Order, plan: ExecutionPlan, intent: TradeIntent,
        market_price: float, correlation_id: str,
    ) -> dict:
        if order.status not in {"SUBMITTED", "NEW", "PARTIALLY_FILLED"}:
            return {"order_id": order.id, "status": order.status, "position_id": order.position_id}
        if not self._should_fill(plan, market_price):
            return {"order_id": order.id, "status": order.status, "position_id": order.position_id}
        return await self._fill(
            order, plan, intent, market_price, correlation_id, perf_counter(),
            fill_remaining=order.status == "PARTIALLY_FILLED",
        )

    async def cancel(self, order: Order, correlation_id: str) -> Order:
        if order.status not in {"SUBMITTED", "NEW", "PARTIALLY_FILLED"}:
            raise SafetyError(f"Order in {order.status} cannot be cancelled")
        order.status = "CANCELLED"
        self._report(order, perf_counter())
        await publish_persisted(
            self.session, self.event_bus,
            Event("OrderCancelled", {"order_id": order.id}, correlation_id), "execution"
        )
        self.session.commit()
        return order

    async def _fill(
        self, order: Order, plan: ExecutionPlan, intent: TradeIntent,
        market_price: float, correlation_id: str, started: float,
        *, fill_remaining: bool = False,
    ) -> dict:
        remaining = decimal(plan.quantity) - decimal(order.fill_quantity)
        ratio = ONE if fill_remaining else decimal(self.settings.paper_partial_fill_ratio)
        fill_quantity = quantity(remaining * ratio)
        if fill_quantity <= ZERO:
            return {"order_id": order.id, "position_id": order.position_id, "idempotent": True}
        fill_price, touch = self.fill_price(plan.side, market_price)
        instrument = self._instrument(plan.symbol)
        if instrument and decimal(instrument.tick_size) > ZERO:
            # Fills land on the venue's tick grid, rounded against the trader.
            tick = decimal(instrument.tick_size)
            steps = fill_price / tick
            steps = steps.to_integral_value(rounding=ROUND_CEILING if plan.side == "BUY" else ROUND_FLOOR)
            fill_price = price(steps * tick)
        fee = money(fill_quantity * fill_price * decimal(self.settings.paper_fee_rate))
        order.slippage_cost = money(decimal(order.slippage_cost or ZERO) + abs(fill_price - touch) * fill_quantity)
        previous_fill = decimal(order.fill_quantity)
        total_fill = quantity(previous_fill + fill_quantity)
        order.average_fill_price = price((
            ((decimal(order.average_fill_price) * previous_fill) if order.average_fill_price else ZERO)
            + fill_price * fill_quantity
        ) / total_fill)
        order.fill_quantity = total_fill
        order.fees = money(decimal(order.fees) + fee)
        order.status = "FILLED" if total_fill >= decimal(plan.quantity) else "PARTIALLY_FILLED"
        execution_key = f"{order.id}:fill:{total_fill}"
        if not self.session.scalar(select(Fill).where(Fill.execution_key == execution_key)):
            self.session.add(Fill(
                order_id=order.id, execution_key=execution_key,
                quantity=fill_quantity, price=fill_price, fee=fee,
            ))
        position = self.session.get(Position, order.position_id) if order.position_id else None
        opened = position is None
        if position:
            prior_quantity = decimal(position.quantity)
            position.entry_price = price(
                (decimal(position.entry_price) * prior_quantity + fill_price * fill_quantity)
                / (prior_quantity + fill_quantity)
            )
            position.quantity = quantity(prior_quantity + fill_quantity)
            position.current_price = fill_price
            position.fees = money(decimal(position.fees) + fee)
        else:
            position = Position(
                strategy_version_id=intent.strategy_version_id, symbol=plan.symbol, side=plan.side,
                quantity=fill_quantity, entry_price=fill_price, current_price=fill_price,
                highest_price=fill_price, lowest_price=fill_price,
                stop_loss=plan.stop_loss, take_profit=plan.take_profit, status="OPEN", fees=fee, venue="paper",
                decision_id=intent.decision_id, slippage_cost=money(abs(fill_price - touch) * fill_quantity),
            )
            self.session.add(position)
            self.session.flush()
            order.position_id = position.id
        self.session.add(PositionEvent(
            position_id=position.id, event_type="OPENED" if opened else "FILL_ADDED",
            from_status="OPENING" if opened else "OPEN", to_status="OPEN",
            price=fill_price, quantity=fill_quantity, reason="paper fill",
        ))
        self.session.flush()
        PaperAccount(self.session, self.settings).snapshot()
        self._report(order, started)
        event_type = "OrderFilled" if order.status == "FILLED" else "OrderPartiallyFilled"
        await publish_persisted(
            self.session, self.event_bus, Event(event_type, {"order_id": order.id}, correlation_id), "execution"
        )
        if opened:
            await publish_persisted(
                self.session, self.event_bus, Event("PositionOpened", {"position_id": position.id}, correlation_id), "positions"
            )
            await publish_persisted(self.session, self.event_bus, Event("TRADE_OPENED", {
                "position_id": position.id, "symbol": plan.symbol, "side": plan.side,
                "strategy_version_id": intent.strategy_version_id, "decision_id": intent.decision_id,
                "execution_plan_id": plan.id, "order_id": order.id, "price": str(fill_price),
            }, correlation_id, source="paper_exchange"), "positions")
        return {"order_id": order.id, "position_id": position.id, "idempotent": False}

    def _report(self, order: Order, started: float) -> None:
        report = self.session.scalar(select(ExecutionReport).where(ExecutionReport.order_id == order.id))
        if not report:
            report = ExecutionReport(order_id=order.id, exchange=order.exchange)
            self.session.add(report)
        report.status = order.status
        report.fill_quantity = order.fill_quantity
        report.average_fill_price = order.average_fill_price
        report.fees = order.fees
        report.execution_time_ms = max(0, round((perf_counter() - started) * 1000))
        report.errors = order.raw_response.get("errors", [])
