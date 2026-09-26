"""Binance Spot Testnet execution venue.

Safety properties (enforced here and by tests):
- The host is a constant pointing at testnet.binance.vision; it is not configurable, so this
  adapter can never reach the real exchange.
- It only uses separate testnet credentials (BINANCE_TESTNET_API_KEY/SECRET), never mainnet keys.
- It consumes an approved ExecutionPlan unchanged, uses idempotent client order IDs, and records
  orders, fills and positions under venue="testnet" so they never mix with paper or live state.
"""

import hashlib
import hmac
from decimal import Decimal
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import ZERO, decimal, money, price, quantity, rate
from app.core.errors import SafetyError
from app.core.events import Event, EventBus, publish_persisted
from app.core.logging import redact
from app.core.time import TimeService
from app.models import (
    ExecutionPlan,
    ExecutionReport,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    PositionEvent,
    TradeIntent,
)
from app.trading.accounts import latest_portfolio

TESTNET_HOST = "https://testnet.binance.vision"
FINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}


class TestnetError(Exception):
    pass


def client_order_id(prefix: str, identifier: str) -> str:
    """Binance allows 36 characters; derive a stable ID so retries are idempotent."""
    return f"{prefix}{identifier.replace('-', '')}"[:36]


class BinanceTestnetExchange:
    venue = "testnet"

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if settings.execution_mode != "testnet":
            raise SafetyError("Testnet venue requires EXECUTION_MODE=testnet")
        if not (settings.binance_testnet_api_key and settings.binance_testnet_api_secret):
            raise SafetyError("Testnet credentials are not configured")
        self.session, self.settings, self.event_bus = session, settings, event_bus
        self.transport = transport

    async def _signed(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urlencode({**params, "timestamp": int(TimeService.now().timestamp() * 1000), "recvWindow": 5000})
        signature = hmac.new(self.settings.binance_testnet_api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(base_url=TESTNET_HOST, timeout=15, transport=self.transport) as client:
                response = await client.request(method, f"{path}?{query}&signature={signature}",
                                                headers={"X-MBX-APIKEY": self.settings.binance_testnet_api_key})
        except httpx.HTTPError as exc:
            raise TestnetError(redact(f"{type(exc).__name__}: {exc}", self.settings.secret_values())) from None
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise TestnetError(redact(f"HTTP {response.status_code}: {payload}", self.settings.secret_values()))
        return payload

    async def sync_portfolio(self) -> PortfolioSnapshot:
        """Testnet balances come from the testnet account, not from paper accounting."""
        account = await self._signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        balances = {item["asset"]: item for item in account.get("balances", [])}
        usdt = balances.get("USDT", {"free": "0", "locked": "0"})
        free, locked = decimal(usdt["free"]), decimal(usdt["locked"])
        open_positions = self.session.scalars(select(Position).where(Position.venue == self.venue,
                                                                     Position.status.in_(["OPEN", "MANAGING"]))).all()
        held = sum((decimal(item.quantity) * decimal(item.current_price) for item in open_positions), ZERO)
        equity = money(free + locked + held)
        previous = latest_portfolio(self.session, self.venue)
        snapshot = PortfolioSnapshot(
            venue=self.venue, equity=equity, available_balance=money(free), margin_used=money(held),
            exposure=rate(held / equity) if equity > ZERO else ZERO,
            daily_pnl=previous.daily_pnl if previous else ZERO, drawdown=previous.drawdown if previous else ZERO,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def _apply_fills(self, order: Order, payload: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
        total_qty, total_quote, total_fee = ZERO, ZERO, ZERO
        for index, fill in enumerate(payload.get("fills", [])):
            fill_qty, fill_price = decimal(fill["qty"]), decimal(fill["price"])
            # Commission may be charged in another asset; convert only quote-asset commissions.
            fee = decimal(fill.get("commission", "0")) if fill.get("commissionAsset") == "USDT" else ZERO
            key = f"testnet:{order.exchange_order_id}:{fill.get('tradeId', index)}"
            if not self.session.scalar(select(Fill.id).where(Fill.execution_key == key)):
                self.session.add(Fill(order_id=order.id, execution_key=key, quantity=fill_qty, price=price(fill_price), fee=money(fee)))
            total_qty += fill_qty
            total_quote += fill_qty * fill_price
            total_fee += fee
        return total_qty, total_quote, total_fee

    async def submit(self, plan: ExecutionPlan, intent: TradeIntent, market_price: float, correlation_id: str) -> dict:
        if plan.exchange != self.venue or plan.order_type != "MARKET":
            raise SafetyError("Testnet venue accepts approved MARKET plans for the testnet venue only")
        coid = client_order_id("qtr", plan.id)
        existing = self.session.scalar(select(Order).where(Order.client_order_id == coid))
        if existing:
            return {"order_id": existing.id, "position_id": existing.position_id, "idempotent": True}
        started = perf_counter()
        order = Order(execution_plan_id=plan.id, exchange=self.venue, client_order_id=coid, exchange_order_id="pending",
                      symbol=plan.symbol, side=plan.side, order_type="MARKET", quantity=plan.quantity,
                      fill_quantity=ZERO, fees=ZERO, status="SUBMITTED", raw_response={"venue": "binance-spot-testnet"},
                      execution_mode="TESTNET", market_data_source="BINANCE_SPOT_TESTNET", reference_price=price(market_price))
        self.session.add(order)
        self.session.flush()
        try:
            payload = await self._signed("POST", "/api/v3/order", {
                "symbol": plan.symbol, "side": plan.side, "type": "MARKET", "quantity": format(decimal(plan.quantity).normalize(), "f"),
                "newClientOrderId": coid, "newOrderRespType": "FULL",
            })
        except TestnetError as exc:
            order.status, order.raw_response = "REJECTED", {"errors": [str(exc)], "venue": "binance-spot-testnet"}
            self._report(order, started)
            return {"order_id": order.id, "position_id": None, "rejected": True, "errors": [str(exc)]}
        order.exchange_order_id = str(payload.get("orderId"))
        filled, quote, fee = self._apply_fills(order, payload)
        order.fill_quantity, order.fees = quantity(filled), money(fee)
        order.average_fill_price = price(quote / filled) if filled > ZERO else None
        order.status = {"FILLED": "FILLED", "PARTIALLY_FILLED": "PARTIALLY_FILLED"}.get(payload.get("status", ""), payload.get("status", "NEW"))
        order.raw_response = {"venue": "binance-spot-testnet", "status": payload.get("status"), "orderId": payload.get("orderId")}
        position = None
        if filled > ZERO:
            fill_price = price(quote / filled)
            position = Position(strategy_version_id=intent.strategy_version_id, symbol=plan.symbol, side=plan.side,
                                quantity=quantity(filled), entry_price=fill_price, current_price=fill_price,
                                highest_price=fill_price, lowest_price=fill_price, stop_loss=plan.stop_loss,
                                take_profit=plan.take_profit, status="OPEN", fees=money(fee), venue=self.venue)
            self.session.add(position)
            self.session.flush()
            order.position_id = position.id
            self.session.add(PositionEvent(position_id=position.id, event_type="OPENED", from_status="OPENING", to_status="OPEN",
                                           price=fill_price, quantity=quantity(filled), reason="testnet fill"))
            await publish_persisted(self.session, self.event_bus, Event("TRADE_OPENED", {
                "position_id": position.id, "venue": self.venue, "symbol": plan.symbol, "order_id": order.id,
                "decision_id": intent.decision_id, "execution_plan_id": plan.id}, correlation_id, source="testnet_exchange"), "positions")
        self._report(order, started)
        return {"order_id": order.id, "position_id": position.id if position else None, "idempotent": False}

    async def close(self, position: Position, reason: str) -> Decimal | None:
        """Protective/strategy exit as a real testnet SELL market order; returns the average fill price."""
        if position.venue != self.venue:
            raise SafetyError("Position does not belong to the testnet venue")
        coid = client_order_id("qtx", position.id)
        payload = await self._signed("POST", "/api/v3/order", {
            "symbol": position.symbol, "side": "SELL" if position.side == "BUY" else "BUY", "type": "MARKET",
            "quantity": format(decimal(position.quantity).normalize(), "f"), "newClientOrderId": coid, "newOrderRespType": "FULL",
        })
        qty = sum((decimal(fill["qty"]) for fill in payload.get("fills", [])), ZERO)
        quote = sum((decimal(fill["qty"]) * decimal(fill["price"]) for fill in payload.get("fills", [])), ZERO)
        return price(quote / qty) if qty > ZERO else None

    async def reconcile(self) -> dict[str, Any]:
        """After a restart, refresh non-final testnet orders from the venue. Never places orders."""
        updated = 0
        for order in self.session.scalars(select(Order).where(Order.exchange == self.venue,
                                                              Order.status.in_(["SUBMITTED", "NEW", "PARTIALLY_FILLED"]))).all():
            try:
                remote = await self._signed("GET", "/api/v3/order", {"symbol": order.symbol, "origClientOrderId": order.client_order_id})
            except TestnetError as exc:
                order.raw_response = {**(order.raw_response or {}), "reconcile_error": str(exc)}
                continue
            order.status = remote.get("status", order.status)
            order.fill_quantity = quantity(decimal(remote.get("executedQty", order.fill_quantity)))
            updated += 1
        self.session.flush()
        return {"orders_checked": updated}

    def _report(self, order: Order, started: float) -> None:
        report = self.session.scalar(select(ExecutionReport).where(ExecutionReport.order_id == order.id))
        if not report:
            report = ExecutionReport(order_id=order.id, exchange=self.venue)
            self.session.add(report)
        report.status, report.fill_quantity = order.status, order.fill_quantity
        report.average_fill_price, report.fees = order.average_fill_price, order.fees
        report.execution_time_ms = max(0, round((perf_counter() - started) * 1000))
        report.errors = (order.raw_response or {}).get("errors", [])
