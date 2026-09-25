"""Immutable trade memory and decision reconstruction."""

from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.decimal_math import ZERO, decimal, money
from app.core.serialization import serialize
from app.core.time import TimeService
from app.models import (
    AIArtifact,
    Decision,
    ExecutionPlan,
    Fill,
    MarketCandle,
    MarketEvent,
    MarketRegimeRecord,
    Opportunity,
    Order,
    Position,
    PositionEvent,
    RiskEvent,
    StrategyVersion,
    TradeIntent,
    TradeMemory,
)


def base_asset(symbol: str) -> str:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "USD"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol


class TradeMemoryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def chain(self, position: Position) -> dict[str, Any]:
        order = self.session.scalar(select(Order).where(Order.position_id == position.id).order_by(Order.created_at))
        plan = self.session.get(ExecutionPlan, order.execution_plan_id) if order else None
        intent = self.session.get(TradeIntent, plan.trade_intent_id) if plan else None
        decision = self.session.get(Decision, intent.decision_id) if intent else None
        return {"order": order, "plan": plan, "intent": intent, "decision": decision}

    def record(self, position: Position) -> TradeMemory | None:
        if position.status != "CLOSED" or position.closed_at is None:
            return None
        existing = self.session.scalar(select(TradeMemory).where(TradeMemory.position_id == position.id))
        if existing:
            return existing  # never overwritten
        chain = self.chain(position)
        order, plan, intent, decision = chain["order"], chain["plan"], chain["intent"], chain["decision"]
        close_event = self.session.scalar(select(PositionEvent).where(
            PositionEvent.position_id == position.id, PositionEvent.event_type == "CLOSED",
        ))
        exit_price = decimal(close_event.price if close_event and close_event.price else position.current_price)
        entry = decimal(position.entry_price)
        # Partial closes reduce position.quantity; the originally filled quantity is the trade size.
        filled = decimal(order.fill_quantity) if order and order.fill_quantity else decimal(position.quantity)
        direction = decimal(1 if position.side == "BUY" else -1)
        fees = decimal(position.fees or 0)
        realized = decimal(position.realized_pnl or 0)
        net = money(realized - fees)
        notional = entry * filled
        reference = decimal(intent.entry_price) if intent else None
        slippage = money((entry - reference) * filled * direction) if reference is not None else ZERO
        stop_distance = abs(entry - decimal(position.stop_loss)) * filled
        opened_at = TimeService.ensure_utc(position.opened_at)
        closed_at = TimeService.ensure_utc(position.closed_at)
        mae, mfe = self._excursions(position, decision, opened_at, closed_at, entry)
        timeframe = decision.timeframe if decision else None
        record = TradeMemory(
            position_id=position.id, symbol=position.symbol, side=position.side, quantity=filled,
            leverage=decimal(plan.leverage) if plan else decimal(1), entry_price=entry, exit_price=exit_price,
            reference_price=reference, fees=money(fees), slippage_cost=slippage, realized_pnl=money(realized),
            net_pnl=net, return_pct=float(net / notional * 100) if notional > 0 else 0.0,
            r_multiple=float(net / stop_distance) if stop_distance > 0 else None,
            mae_pct=mae, mfe_pct=mfe,
            holding_seconds=max(0, int((closed_at - opened_at).total_seconds())),
            exit_reason=position.exit_reason or "unknown", strategy_version_id=position.strategy_version_id,
            decision_id=decision.id if decision else None, trade_intent_id=intent.id if intent else None,
            execution_plan_id=plan.id if plan else None, timeframe=timeframe,
            regime_at_entry=self._entry_regime(decision), regime_at_exit=self._regime_at(position.symbol, timeframe, closed_at),
            event_ids=self._events(position.symbol, opened_at, closed_at),
            stop_loss=decimal(position.stop_loss), take_profit=decimal(position.take_profit),
            opened_at=opened_at, closed_at=closed_at,
            lineage=(decision.lineage or {}) if decision else {},
        )
        self.session.add(record)
        self.session.flush()
        return record

    @staticmethod
    def _entry_regime(decision: Decision | None) -> str | None:
        if not decision:
            return None
        context = decision.market_context or {}
        return context.get("market_regime") or context.get("regime")

    def _regime_at(self, symbol: str, timeframe: str | None, at: Any) -> str | None:
        query = select(MarketRegimeRecord).where(MarketRegimeRecord.symbol == symbol, MarketRegimeRecord.candle_timestamp <= at)
        if timeframe:
            query = query.where(MarketRegimeRecord.timeframe == timeframe)
        record = self.session.scalar(query.order_by(desc(MarketRegimeRecord.candle_timestamp)))
        return record.regime if record else None

    def _events(self, symbol: str, opened_at: Any, closed_at: Any) -> list[str]:
        base = base_asset(symbol)
        rows = self.session.scalars(select(MarketEvent).where(
            MarketEvent.event_at >= opened_at - timedelta(hours=24), MarketEvent.event_at <= closed_at,
        ).limit(500)).all()
        return [row.id for row in rows if base in (row.affected_assets or []) or "RISK_ASSETS" in (row.affected_assets or [])]

    def _excursions(self, position: Position, decision: Decision | None, opened_at: Any, closed_at: Any,
                    entry: Any) -> tuple[float | None, float | None]:
        highs = [decimal(position.highest_price)] if position.highest_price is not None else []
        lows = [decimal(position.lowest_price)] if position.lowest_price is not None else []
        query = select(MarketCandle).where(
            MarketCandle.symbol == position.symbol, MarketCandle.timestamp >= opened_at, MarketCandle.timestamp <= closed_at,
        )
        if decision and decision.exchange:
            query = query.where(MarketCandle.exchange == decision.exchange)
        if decision and decision.timeframe:
            query = query.where(MarketCandle.timeframe == decision.timeframe)
        for candle in self.session.scalars(query.limit(5000)).all():
            highs.append(decimal(candle.high))
            lows.append(decimal(candle.low))
        highs.append(decimal(position.current_price))
        lows.append(decimal(position.current_price))
        if entry <= 0:
            return None, None
        high, low = max(highs), min(lows)
        if position.side == "BUY":
            return float((low / entry - 1) * 100), float((high / entry - 1) * 100)
        return float((1 - high / entry) * 100), float((1 - low / entry) * 100)


def reconstruct_decision(session: Session, decision_id: str) -> dict[str, Any] | None:
    """Everything known at decision time plus what happened afterwards, by reference."""
    decision = session.get(Decision, decision_id)
    if not decision:
        return None
    intents = session.scalars(select(TradeIntent).where(TradeIntent.decision_id == decision.id)).all()
    intent_ids = [item.id for item in intents]
    plans = session.scalars(select(ExecutionPlan).where(ExecutionPlan.trade_intent_id.in_(intent_ids))).all() if intent_ids else []
    risk = session.scalars(select(RiskEvent).where(RiskEvent.trade_intent_id.in_(intent_ids))).all() if intent_ids else []
    plan_ids = [item.id for item in plans]
    orders = session.scalars(select(Order).where(Order.execution_plan_id.in_(plan_ids))).all() if plan_ids else []
    order_ids = [item.id for item in orders]
    fills = session.scalars(select(Fill).where(Fill.order_id.in_(order_ids))).all() if order_ids else []
    positions = [session.get(Position, item.position_id) for item in orders if item.position_id]
    lineage = decision.lineage or {}
    versions = session.scalars(select(StrategyVersion).where(
        StrategyVersion.id.in_([item["id"] for item in lineage.get("strategy_versions", [])])
    )).all() if lineage.get("strategy_versions") else []
    events = session.scalars(select(MarketEvent).where(MarketEvent.id.in_(lineage.get("event_ids", [])))).all() if lineage.get("event_ids") else []
    artifacts = session.scalars(select(AIArtifact).where(AIArtifact.id.in_(lineage.get("ai_artifact_ids", [])))).all() if lineage.get("ai_artifact_ids") else []
    opportunity = session.get(Opportunity, decision.selected_opportunity_id) if decision.selected_opportunity_id else None
    trades = session.scalars(select(TradeMemory).where(TradeMemory.decision_id == decision.id)).all()
    return {
        "decision": serialize(decision),
        "strategy_versions": [serialize(item) for item in versions],
        "selected_opportunity": serialize(opportunity) if opportunity else None,
        "news_available": [serialize(item) for item in events],
        "ai_analysis": [serialize(item) for item in artifacts],
        "trade_intents": [serialize(item) for item in intents],
        "risk_assessments": [serialize(item) for item in risk],
        "execution_plans": [serialize(item) for item in plans],
        "orders": [serialize(item) for item in orders],
        "fills": [serialize(item) for item in fills],
        "positions": [serialize(item) for item in positions if item],
        "trade_memory": [serialize(item) for item in trades],
    }
