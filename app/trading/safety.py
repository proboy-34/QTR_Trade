"""Global safety layer and kill switch.

Scopes: EMERGENCY and SYSTEM (global), PAPER_TRADING and NEW_ORDERS (execution),
STRATEGY:<id>, ASSET:<symbol>. Any active
control blocks new trades. Protective exits (stops/targets) keep running, because
closing risk is always allowed. Automatic triggers persist until an operator clears them.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import decimal
from app.core.events import Event, EventBus, publish_persisted
from app.core.time import TimeService
from app.models import Order, PortfolioSnapshot, Position, SafetyControl
from app.trading.accounts import VENUES, latest_portfolio

SCOPES = ("EMERGENCY", "SYSTEM", "PAPER_TRADING", "NEW_ORDERS", "STRATEGY", "ASSET")
GLOBAL_BLOCKING = ("EMERGENCY", "SYSTEM", "PAPER_TRADING", "NEW_ORDERS")
HALTING = ("EMERGENCY", "SYSTEM")


class SafetyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def active(self) -> list[SafetyControl]:
        return list(self.session.scalars(select(SafetyControl).where(SafetyControl.active.is_(True))
                                         .order_by(SafetyControl.triggered_at)).all())

    def blocks_new_orders(self, strategy_id: str | None = None, symbol: str | None = None) -> list[str]:
        reasons = []
        for control in self.active():
            if control.scope in GLOBAL_BLOCKING:
                reasons.append(f"SAFETY_{control.scope}_STOP")
            elif control.scope == "STRATEGY" and strategy_id and control.target == strategy_id:
                reasons.append("SAFETY_STRATEGY_STOP")
            elif control.scope == "ASSET" and symbol and control.target == symbol:
                reasons.append("SAFETY_ASSET_STOP")
        return sorted(set(reasons))

    def evaluation_halted(self) -> list[str]:
        return [f"SAFETY_{c.scope}_STOP" for c in self.active() if c.scope in {"EMERGENCY", "SYSTEM", "PAPER_TRADING"}]

    def activate(self, scope: str, reason: str, *, target: str = "*", trigger: str = "OPERATOR",
                 source: str = "operator", details: dict[str, Any] | None = None) -> tuple[SafetyControl, bool]:
        if scope not in SCOPES:
            raise ValueError(f"Unknown safety scope {scope}")
        if scope in {"STRATEGY", "ASSET"} and target == "*":
            raise ValueError(f"{scope} stop requires a target")
        existing = self.session.scalar(select(SafetyControl).where(
            SafetyControl.active.is_(True), SafetyControl.scope == scope, SafetyControl.target == target,
            SafetyControl.trigger == trigger,
        ))
        if existing:
            return existing, False
        control = SafetyControl(scope=scope, target=target, reason=reason, trigger=trigger, source=source,
                                details=details or {})
        self.session.add(control)
        self.session.flush()
        return control, True

    def clear(self, control_id: str, cleared_by: str) -> SafetyControl | None:
        control = self.session.get(SafetyControl, control_id)
        if control and control.active:
            control.active = False
            control.cleared_at = TimeService.now()
            control.cleared_by = cleared_by
            self.session.flush()
        return control


class SafetyMonitor:
    """Automatic safe-mode triggers evaluated on a schedule."""

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus
        self.safety = SafetyService(session)

    async def _trigger(self, scope: str, trigger: str, reason: str, target: str = "*", **details: Any) -> dict[str, Any] | None:
        control, created = self.safety.activate(scope, reason, target=target, trigger=trigger, source="automatic", details=details)
        if not created:
            return None
        await publish_persisted(self.session, self.event_bus, Event("SAFE_MODE_TRIGGERED", {
            "control_id": control.id, "scope": scope, "target": target, "trigger": trigger, "message": reason,
        }, source="safety_monitor"), "safety")
        return {"control_id": control.id, "scope": scope, "trigger": trigger}

    async def _portfolio_checks(self, latest: PortfolioSnapshot | None, venue: str, triggered: list) -> None:
        if latest is None:
            return
        equity = decimal(latest.equity)
        if equity > 0 and decimal(latest.daily_pnl) / equity <= -decimal(self.settings.max_daily_loss):
            triggered.append(await self._trigger("NEW_ORDERS", "EXCESSIVE_DAILY_LOSS", f"Daily loss limit breached ({venue})",
                                                 venue=venue, daily_pnl=str(latest.daily_pnl)))
        if decimal(latest.drawdown) >= decimal(self.settings.max_drawdown):
            triggered.append(await self._trigger("NEW_ORDERS", "EXCESSIVE_DRAWDOWN", f"Maximum drawdown breached ({venue})",
                                                 venue=venue, drawdown=str(latest.drawdown)))
        if equity <= 0:
            triggered.append(await self._trigger("PAPER_TRADING", "CORRUPTED_STATE", f"Non-positive {venue} equity", venue=venue))

    async def check(self, live_state: dict[str, Any] | None = None) -> dict[str, Any]:
        triggered: list[dict[str, Any] | None] = []
        now = TimeService.now()
        for venue in VENUES:
            await self._portfolio_checks(latest_portfolio(self.session, venue), venue, triggered)
        corrupted = self.session.scalar(select(func.count()).select_from(Position).where(
            Position.status.in_(["OPEN", "MANAGING"]),
            (Position.quantity <= 0) | (Position.entry_price <= 0),
        )) or 0
        if corrupted:
            triggered.append(await self._trigger("PAPER_TRADING", "INCONSISTENT_POSITION",
                                                 f"{corrupted} open position(s) with invalid quantity or price"))
        errors = self.session.scalar(select(func.count()).select_from(Order).where(
            Order.status == "REJECTED", Order.created_at >= now - timedelta(hours=1),
        )) or 0
        if errors >= self.settings.safety_max_execution_errors:
            triggered.append(await self._trigger("NEW_ORDERS", "REPEATED_EXECUTION_ERRORS",
                                                 f"{errors} rejected paper orders in the last hour"))
        if live_state and live_state.get("running"):
            last = live_state.get("last_message_at")
            if last:
                age = (now - TimeService.ensure_utc(datetime.fromisoformat(last))).total_seconds()
                if age > self.settings.safety_max_stale_seconds:
                    triggered.append(await self._trigger("NEW_ORDERS", "STALE_MARKET_DATA",
                                                         f"No live market message for {int(age)}s", age_seconds=int(age)))
            if live_state.get("status") == "ERROR":
                triggered.append(await self._trigger("NEW_ORDERS", "PROVIDER_FAILURE",
                                                     f"Live data provider error: {live_state.get('last_error')}"))
        self.session.commit()
        return {"triggered": [item for item in triggered if item], "active": len(self.safety.active())}
