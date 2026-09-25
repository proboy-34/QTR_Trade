from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.core.time import TimeService
from app.global_services.market_data import MarketDataQuality
from app.models import (
    Decision,
    ExecutionPlan,
    MarketContextRecord,
    Opportunity,
    Strategy,
    StrategyVersion,
    TradeIntent,
)
from app.trading.paper_exchange import PaperExchange
from app.trading.risk import RiskEngine


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    volume: float
    volatility: float
    funding: float
    regime: str
    direction: str
    liquidity: str = "STRONG"
    observed_at: datetime = field(default_factory=TimeService.now)
    exchange: str = "paper"
    timeframe: str = "1h"


class TradingPipeline:
    """Coordinates layers while keeping each layer's question and output distinct."""

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session, self.settings, self.event_bus = session, settings, event_bus

    async def evaluate(self, snapshot: MarketSnapshot, force_signal: bool = False) -> dict:
        correlation_id = str(uuid4())
        try:
            MarketDataQuality().validate_snapshot(snapshot.price, snapshot.volume, snapshot.observed_at)
        except ValueError as exc:
            decision = self._decision(
                snapshot, "IGNORE", [], 0, [f"Market snapshot rejected: {exc}"], correlation_id
            )
            self.session.commit()
            return {"decision_id": decision.id, "outcome": "IGNORE", "reasoning": decision.reasoning}

        context = self._remember_context(snapshot)
        strategies = self.session.scalars(
            select(Strategy).where(
                Strategy.symbol == snapshot.symbol,
                Strategy.timeframe == snapshot.timeframe,
                Strategy.status == "active",
            )
        ).all()
        evaluations: list[dict[str, Any]] = []
        selected: tuple[Strategy, StrategyVersion, Opportunity] | None = None
        for strategy in strategies:
            version = max(strategy.versions, key=lambda item: item.version)
            filters = version.filters or {}
            regime_ok = not filters.get("regime") or snapshot.regime in filters["regime"]
            direction_ok = snapshot.direction == "BULLISH"
            liquidity_ok = snapshot.liquidity == "STRONG"
            score = int(regime_ok) * 45 + int(direction_ok) * 35 + int(liquidity_ok) * 20
            eligible = regime_ok and liquidity_ok and score >= 75
            reasons = [
                f"regime_match={regime_ok}",
                f"direction_bullish={direction_ok}",
                f"liquidity_strong={liquidity_ok}",
            ]
            opportunity = Opportunity(
                market_context_id=context.id,
                strategy_version_id=version.id,
                symbol=snapshot.symbol,
                status="EVALUATING",
                score=score,
                reasons=reasons,
                expires_at=TimeService.now() + timedelta(minutes=5),
            )
            self.session.add(opportunity)
            self.session.flush()
            opportunity.status = (
                "SELECTED"
                if eligible and (force_signal or direction_ok) and selected is None
                else "REJECTED"
            )
            evaluations.append({
                "opportunity_id": opportunity.id,
                "strategy_id": strategy.id,
                "strategy": strategy.name,
                "version": version.version,
                "eligible": eligible,
                "score": score,
                "reasons": reasons,
            })
            if opportunity.status == "SELECTED":
                selected = strategy, version, opportunity

        outcome = "TRADE" if selected else "WAIT"
        reasoning = [
            "Eligible deterministic strategy and entry context found"
            if selected
            else "No active strategy currently satisfies every eligibility and entry condition"
        ]
        decision = self._decision(
            snapshot,
            outcome,
            evaluations,
            max((int(item["score"]) for item in evaluations), default=0) / 100,
            reasoning,
            correlation_id,
            selected[2].id if selected else None,
        )
        if not selected:
            self.session.commit()
            return {"decision_id": decision.id, "outcome": outcome, "reasoning": reasoning}

        _, version, _ = selected
        intent = TradeIntent(
            decision_id=decision.id,
            strategy_version_id=version.id,
            symbol=snapshot.symbol,
            side="BUY",
            entry_price=snapshot.price,
            stop_loss=None,
            take_profit=None,
            confidence=decision.confidence,
            status="created",
        )
        self.session.add(intent)
        self.session.flush()
        await publish_persisted(
            self.session,
            self.event_bus,
            Event("TradeIntentCreated", {"trade_intent_id": intent.id}, correlation_id),
            "decision",
        )
        assessment = RiskEngine(self.session, self.settings).assess(
            intent, snapshot.price, snapshot.volatility
        )
        if not assessment.approved:
            await publish_persisted(
                self.session,
                self.event_bus,
                Event(
                    "RiskRejected",
                    {"trade_intent_id": intent.id, "reasons": assessment.reason_codes},
                    correlation_id,
                    source="risk_engine",
                ),
                "portfolio_risk",
            )
            self.session.commit()
            return {
                "decision_id": decision.id,
                "outcome": outcome,
                "trade_intent_id": intent.id,
                "risk_outcome": "REJECTED",
                "risk_reasons": assessment.reason_codes,
            }
        plan = ExecutionPlan(
            trade_intent_id=intent.id,
            exchange="paper",
            symbol=intent.symbol,
            side=intent.side,
            quantity=assessment.quantity,
            order_type="MARKET",
            risk_amount=assessment.risk_amount,
            stop_loss=assessment.stop_loss,
            take_profit=assessment.take_profit,
            leverage=assessment.leverage,
            status="approved",
        )
        self.session.add(plan)
        self.session.flush()
        await publish_persisted(
            self.session,
            self.event_bus,
            Event("ExecutionPlanCreated", {"execution_plan_id": plan.id}, correlation_id),
            "portfolio_risk",
        )
        execution = await PaperExchange(self.session, self.settings, self.event_bus).submit(
            plan, intent, snapshot.price, correlation_id
        )
        intent.status = "executed" if execution.get("position_id") else "submitted"
        self.session.commit()
        return {
            "decision_id": decision.id,
            "outcome": outcome,
            "trade_intent_id": intent.id,
            "risk_outcome": "APPROVED",
            "execution_plan_id": plan.id,
            **execution,
        }

    def _remember_context(self, snapshot: MarketSnapshot) -> MarketContextRecord:
        previous = self.session.scalar(
            select(MarketContextRecord)
            .where(MarketContextRecord.symbol == snapshot.symbol)
            .order_by(MarketContextRecord.observed_at.desc())
        )
        context = MarketContextRecord(
            symbol=snapshot.symbol,
            price=snapshot.price,
            regime=snapshot.regime,
            direction=snapshot.direction,
            volatility=snapshot.volatility,
            liquidity=snapshot.liquidity,
            funding=snapshot.funding,
            previous_regime=previous.regime if previous else None,
            snapshot={
                key: str(value) if isinstance(value, datetime) else value
                for key, value in asdict(snapshot).items()
            },
            observed_at=snapshot.observed_at,
        )
        self.session.add(context)
        self.session.flush()
        return context

    def _decision(
        self,
        snapshot: MarketSnapshot,
        outcome: str,
        evaluations: list[dict[str, Any]],
        confidence: float,
        reasoning: list[str],
        correlation_id: str,
        selected_opportunity_id: str | None = None,
    ) -> Decision:
        context = {
            key: str(value) if isinstance(value, datetime) else value
            for key, value in asdict(snapshot).items()
        }
        decision = Decision(
            symbol=snapshot.symbol,
            outcome=outcome,
            market_context=context,
            evaluations=evaluations,
            confidence=confidence,
            reasoning=reasoning,
            correlation_id=correlation_id,
            selected_opportunity_id=selected_opportunity_id,
        )
        self.session.add(decision)
        self.session.flush()
        return decision
