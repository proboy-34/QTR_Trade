from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.core.lineage import code_version, risk_configuration
from app.core.time import TimeService
from app.global_services.historical import TIMEFRAME_DELTA
from app.global_services.market_data import MarketDataQuality
from app.global_services.regime import candles_frame
from app.global_services.universe import UniverseService
from app.memory.trade_memory import base_asset
from app.models import (
    AIArtifact,
    Decision,
    ExecutionPlan,
    MarketCandle,
    MarketContextRecord,
    MarketEvent,
    MarketRegimeRecord,
    Opportunity,
    PortfolioSnapshot,
    Strategy,
    StrategyVersion,
    TradeIntent,
)
from app.research.dsl import CompiledStrategy, StrategySpec, spec_from_version
from app.trading.paper_exchange import PaperExchange
from app.trading.risk import RiskEngine
from app.trading.safety import SafetyService


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
    market_regime: str | None = None


# Strategies the Decision layer may read: operator-activated ones and research candidates
# in paper testing (all execution is paper). Research never calls this layer directly.
DECIDABLE_STATUSES = ("active", "paper_testing")


def strategy_covers(strategy: Strategy, version: StrategyVersion, symbol: str, eligible: set[str]) -> bool:
    universe = [item.upper() for item in (version.filters or {}).get("universe", [])]
    return strategy.symbol == symbol or symbol in universe or ("ELIGIBLE" in universe and symbol in eligible)


def latest_signals(session: Session, spec: StrategySpec, snapshot: MarketSnapshot) -> tuple[dict[str, bool] | None, str]:
    """Entry/exit flags of a specification on the latest stored closed candle at snapshot time."""
    compiled = CompiledStrategy(spec)
    rows = session.scalars(select(MarketCandle).where(
        MarketCandle.exchange == snapshot.exchange, MarketCandle.symbol == snapshot.symbol,
        MarketCandle.timeframe == snapshot.timeframe, MarketCandle.timestamp <= snapshot.observed_at,
    ).order_by(desc(MarketCandle.timestamp)).limit(max(400, compiled.warmup() + 5))).all()
    if len(rows) < compiled.warmup() + 2:
        return None, "insufficient_candles"
    interval = TIMEFRAME_DELTA.get(snapshot.timeframe, timedelta(hours=1))
    if TimeService.ensure_utc(snapshot.observed_at) - TimeService.ensure_utc(rows[0].timestamp) > interval * 3:
        return None, "stale_candles"
    flags = compiled.signals(candles_frame(list(reversed(rows)))).iloc[-1]
    return {"entry": bool(flags["entry"]), "exit": bool(flags["exit"])}, "ok"


class TradingPipeline:
    """Coordinates layers while keeping each layer's question and output distinct."""

    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session, self.settings, self.event_bus = session, settings, event_bus

    def _entry_signal(self, strategy: Strategy, version: StrategyVersion, snapshot: MarketSnapshot) -> tuple[bool | None, str]:
        """Evaluate a declarative entry rule on stored closed candles known at decision time."""
        spec = spec_from_version(version, strategy)
        if spec is None:
            return None, "legacy_direction"
        if snapshot.timeframe not in spec.timeframes:
            return False, "timeframe_not_supported"
        signals, status = latest_signals(self.session, spec, snapshot)
        if signals is None:
            return (False, status) if status == "stale_candles" else (None, "legacy_direction_insufficient_candles")
        return bool(signals["entry"]), "declarative_rule"

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
        halted = SafetyService(self.session).evaluation_halted()
        if halted:
            decision = self._decision(
                snapshot, "IGNORE", [], 0, [f"Safety control active: {', '.join(halted)}"], correlation_id
            )
            self.session.commit()
            return {"decision_id": decision.id, "outcome": "IGNORE", "reasoning": decision.reasoning}

        if snapshot.market_regime is None:
            regime_record = self.session.scalar(select(MarketRegimeRecord).where(
                MarketRegimeRecord.exchange == snapshot.exchange, MarketRegimeRecord.symbol == snapshot.symbol,
                MarketRegimeRecord.timeframe == snapshot.timeframe,
            ).order_by(desc(MarketRegimeRecord.candle_timestamp)))
            snapshot.market_regime = regime_record.regime if regime_record else None
        context = self._remember_context(snapshot)
        eligible_symbols = set(UniverseService.eligible_symbols(self.session, snapshot.exchange))
        candidates = self.session.scalars(
            select(Strategy).where(
                Strategy.timeframe == snapshot.timeframe,
                Strategy.status.in_(DECIDABLE_STATUSES),
            )
        ).all()
        strategies = [
            strategy for strategy in candidates if strategy.versions and strategy_covers(
                strategy, max(strategy.versions, key=lambda item: item.version), snapshot.symbol, eligible_symbols)
        ]
        evaluations: list[dict[str, Any]] = []
        selected: tuple[Strategy, StrategyVersion, Opportunity] | None = None
        for strategy in strategies:
            version = max(strategy.versions, key=lambda item: item.version)
            filters = version.filters or {}
            regime_ok = not filters.get("regime") or snapshot.regime in filters["regime"]
            if filters.get("market_regimes"):
                regime_ok = regime_ok and snapshot.market_regime in filters["market_regimes"]
            signal, signal_source = self._entry_signal(strategy, version, snapshot)
            direction_ok = snapshot.direction == "BULLISH" if signal is None else signal
            liquidity_ok = snapshot.liquidity == "STRONG"
            score = int(regime_ok) * 45 + int(direction_ok) * 35 + int(liquidity_ok) * 20
            eligible = regime_ok and liquidity_ok and score >= 75
            reasons = [
                f"regime_match={regime_ok}",
                f"entry_signal={direction_ok}" if signal is not None else f"direction_bullish={direction_ok}",
                f"liquidity_strong={liquidity_ok}",
                f"signal_source={signal_source}",
                f"strategy_status={strategy.status}",
            ]
            opportunity = Opportunity(
                market_context_id=context.id,
                strategy_version_id=version.id,
                symbol=snapshot.symbol,
                status="EVALUATING",
                score=score,
                reasons=reasons,
                expires_at=TimeService.now() + timedelta(minutes=5),
                source="decision", exchange=snapshot.exchange, timeframe=snapshot.timeframe,
                regime=snapshot.market_regime or snapshot.regime,
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
                "strategy_version_id": version.id,
                "content_hash": version.content_hash,
                "signal_source": signal_source,
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
        await publish_persisted(self.session, self.event_bus, Event("DECISION_CREATED", {
            "decision_id": decision.id, "symbol": snapshot.symbol, "timeframe": snapshot.timeframe,
            "outcome": outcome, "strategies_evaluated": len(evaluations),
        }, correlation_id, source="decision"), "decision")
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
            exchange=snapshot.exchange,
            timeframe=snapshot.timeframe,
            market_regime=snapshot.market_regime,
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
            exchange=snapshot.exchange,
            timeframe=snapshot.timeframe,
            lineage=self._lineage(snapshot, evaluations),
        )
        self.session.add(decision)
        self.session.flush()
        return decision

    def _lineage(self, snapshot: MarketSnapshot, evaluations: list[dict[str, Any]]) -> dict[str, Any]:
        """References to every input available at decision time (reconstructable later)."""
        observed = TimeService.ensure_utc(snapshot.observed_at)
        portfolio = self.session.scalar(select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc()))
        last_candle = self.session.scalar(select(MarketCandle.timestamp).where(
            MarketCandle.exchange == snapshot.exchange, MarketCandle.symbol == snapshot.symbol,
            MarketCandle.timeframe == snapshot.timeframe, MarketCandle.timestamp <= observed,
        ).order_by(desc(MarketCandle.timestamp)))
        base = base_asset(snapshot.symbol)
        events = self.session.scalars(select(MarketEvent).where(
            MarketEvent.event_at >= observed - timedelta(hours=24), MarketEvent.event_at <= observed + timedelta(hours=1),
        ).limit(500)).all()
        event_ids = [item.id for item in events
                     if base in (item.affected_assets or []) or "RISK_ASSETS" in (item.affected_assets or [])]
        regime = self.session.scalar(select(MarketRegimeRecord.id).where(
            MarketRegimeRecord.exchange == snapshot.exchange, MarketRegimeRecord.symbol == snapshot.symbol,
            MarketRegimeRecord.timeframe == snapshot.timeframe,
        ).order_by(desc(MarketRegimeRecord.candle_timestamp)))
        artifacts = self.session.scalars(select(AIArtifact.id).where(
            AIArtifact.subject_id == snapshot.symbol, AIArtifact.created_at >= observed - timedelta(hours=24),
        ).limit(10)).all()
        return {
            "code_version": code_version(),
            "market_data": {"exchange": snapshot.exchange, "symbol": snapshot.symbol, "timeframe": snapshot.timeframe,
                            "observed_at": observed.isoformat(),
                            "last_candle_at": last_candle.isoformat() if last_candle else None},
            "strategy_versions": [{"id": item["strategy_version_id"], "content_hash": item["content_hash"]}
                                  for item in evaluations if item.get("strategy_version_id")],
            "portfolio_snapshot_id": portfolio.id if portfolio else None,
            "risk_configuration": risk_configuration(self.settings),
            "event_ids": event_ids,
            "regime_record_id": regime,
            "ai_artifact_ids": list(artifacts),
            "ai_role": "context only; AI output never selects trades",
        }
