"""'Why did we not trade?' memory and counterfactual outcome evaluation.

Counterfactuals are research evidence only. They never modify strategies or risk rules.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.decimal_math import decimal
from app.core.time import TimeService
from app.global_services.historical import TIMEFRAME_DELTA
from app.memory.knowledge import KnowledgeBase
from app.models import (
    CounterfactualEvaluation,
    Decision,
    KnowledgeEntry,
    MarketCandle,
    MarketContextRecord,
    Opportunity,
    RiskEvent,
    TradeIntent,
)

MOVE_THRESHOLD_PCT = 2.0


class CounterfactualService:
    def __init__(self, session: Session, horizon_bars: int = 12) -> None:
        self.session = session
        self.horizon_bars = horizon_bars

    def _exists(self, **criteria: Any) -> bool:
        query = select(CounterfactualEvaluation.id)
        for key, value in criteria.items():
            query = query.where(getattr(CounterfactualEvaluation, key) == value)
        return self.session.scalar(query) is not None

    def capture(self, limit: int = 500) -> int:
        """Record rejected/unacted opportunities with the information available at the time."""
        created = 0
        # Risk-rejected trade intents: a valid signal that portfolio/risk declined.
        rejected = self.session.execute(
            select(RiskEvent, TradeIntent, Decision)
            .join(TradeIntent, TradeIntent.id == RiskEvent.trade_intent_id)
            .join(Decision, Decision.id == TradeIntent.decision_id)
            .where(RiskEvent.outcome == "REJECTED").limit(limit)
        ).all()
        for risk, intent, decision in rejected:
            if self._exists(decision_id=decision.id):
                continue
            self._add(decision_id=decision.id, opportunity_id=decision.selected_opportunity_id,
                      exchange=decision.exchange or (decision.market_context or {}).get("exchange", "paper"),
                      symbol=intent.symbol, timeframe=decision.timeframe or (decision.market_context or {}).get("timeframe", "1h"),
                      outcome="TRADE", category="RISK_REJECTED", reasons=list(risk.reason_codes or []),
                      price=intent.entry_price, at=decision.created_at)
            created += 1
        # Scanner opportunities that expired without any trade decision for the symbol.
        stale = self.session.scalars(select(Opportunity).where(
            Opportunity.source == "scanner", Opportunity.status.in_(["EXPIRED", "DISMISSED"]),
        ).limit(limit)).all()
        for opportunity in stale:
            if self._exists(opportunity_id=opportunity.id):
                continue
            context = self.session.get(MarketContextRecord, opportunity.market_context_id)
            if not context:
                continue
            traded = self.session.scalar(select(Decision.id).where(
                Decision.symbol == opportunity.symbol, Decision.outcome == "TRADE",
                Decision.created_at >= opportunity.created_at,
                Decision.created_at <= (opportunity.expires_at or opportunity.created_at),
            ))
            if traded:
                continue
            self._add(decision_id=None, opportunity_id=opportunity.id, exchange=opportunity.exchange or "binance",
                      symbol=opportunity.symbol, timeframe=opportunity.timeframe or "1h", outcome="NOT_ACTED",
                      category="NO_ELIGIBLE_STRATEGY", reasons=list(opportunity.reasons or []),
                      price=context.price, at=context.observed_at)
            created += 1
        # Near-miss strategy rejections (high score but not selected).
        near = self.session.scalars(select(Opportunity).where(
            Opportunity.source == "decision", Opportunity.status == "REJECTED", Opportunity.score >= 65,
        ).limit(limit)).all()
        for opportunity in near:
            if self._exists(opportunity_id=opportunity.id):
                continue
            context = self.session.get(MarketContextRecord, opportunity.market_context_id)
            if not context:
                continue
            snapshot = context.snapshot or {}
            self._add(decision_id=None, opportunity_id=opportunity.id, exchange=snapshot.get("exchange", context.exchange or "paper"),
                      symbol=opportunity.symbol, timeframe=snapshot.get("timeframe", context.timeframe or "1h"),
                      outcome="WAIT", category="STRATEGY_REJECTED", reasons=list(opportunity.reasons or []),
                      price=context.price, at=context.observed_at)
            created += 1
        self.session.flush()
        return created

    def _add(self, *, decision_id: str | None, opportunity_id: str | None, exchange: str, symbol: str,
             timeframe: str, outcome: str, category: str, reasons: list[str], price: Any, at: Any) -> None:
        self.session.add(CounterfactualEvaluation(
            decision_id=decision_id, opportunity_id=opportunity_id, exchange=exchange, symbol=symbol,
            timeframe=timeframe, decision_outcome=outcome, rejection_category=category,
            rejection_reasons=reasons, reference_price=decimal(price), reference_at=TimeService.ensure_utc(at),
            horizon_bars=self.horizon_bars, status="PENDING",
        ))

    def evaluate_pending(self, limit: int = 500) -> dict[str, int]:
        counts = {"evaluated": 0, "insufficient": 0, "waiting": 0}
        now = TimeService.now()
        pending = self.session.scalars(select(CounterfactualEvaluation).where(
            CounterfactualEvaluation.status == "PENDING"
        ).limit(limit)).all()
        for item in pending:
            interval = TIMEFRAME_DELTA.get(item.timeframe, timedelta(hours=1))
            start = TimeService.ensure_utc(item.reference_at)
            end = start + interval * item.horizon_bars
            if now < end + interval:
                counts["waiting"] += 1
                continue
            candles = self.session.scalars(select(MarketCandle).where(
                MarketCandle.exchange == item.exchange, MarketCandle.symbol == item.symbol,
                MarketCandle.timeframe == item.timeframe, MarketCandle.timestamp > start, MarketCandle.timestamp <= end,
            ).order_by(MarketCandle.timestamp)).all()
            if len(candles) < max(1, item.horizon_bars // 2):
                if now > end + interval * item.horizon_bars * 2:
                    item.status, item.evaluated_at = "INSUFFICIENT_DATA", now
                    counts["insufficient"] += 1
                else:
                    counts["waiting"] += 1
                continue
            reference = float(item.reference_price)
            item.forward_return_pct = round((float(candles[-1].close) / reference - 1) * 100, 4)
            item.max_up_pct = round((max(float(c.high) for c in candles) / reference - 1) * 100, 4)
            item.max_down_pct = round((min(float(c.low) for c in candles) / reference - 1) * 100, 4)
            if item.forward_return_pct >= MOVE_THRESHOLD_PCT:
                item.verdict = "MISSED_OPPORTUNITY"
            elif item.forward_return_pct <= -MOVE_THRESHOLD_PCT:
                item.verdict = "CORRECT_AVOIDANCE"
            else:
                item.verdict = "NEUTRAL"
            item.status, item.evaluated_at = "EVALUATED", now
            counts["evaluated"] += 1
        self.session.flush()
        self._findings()
        return counts

    def _findings(self, minimum: int = 10) -> None:
        rows = self.session.execute(
            select(CounterfactualEvaluation.rejection_category, CounterfactualEvaluation.verdict, func.count())
            .where(CounterfactualEvaluation.status == "EVALUATED")
            .group_by(CounterfactualEvaluation.rejection_category, CounterfactualEvaluation.verdict)
        ).all()
        summary: dict[str, dict[str, int]] = {}
        for category, verdict, count in rows:
            summary.setdefault(category, {})[verdict or "UNKNOWN"] = count
        for category, verdicts in summary.items():
            total = sum(verdicts.values())
            if total < minimum:
                continue
            existing = self.session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.dedup_key == f"counterfactual:{category}"))
            if existing and (existing.evidence or {}).get("total") == total:
                continue  # no new evidence since the last finding
            missed = verdicts.get("MISSED_OPPORTUNITY", 0) / total
            avoided = verdicts.get("CORRECT_AVOIDANCE", 0) / total
            KnowledgeBase(self.session).record(
                "COUNTERFACTUAL_FINDING", f"{category}: {missed:.0%} missed moves, {avoided:.0%} correct avoidances ({total} cases)",
                body="Aggregated counterfactual evidence. Informational only; no rule was changed.",
                evidence={"verdicts": verdicts, "total": total, "threshold_pct": MOVE_THRESHOLD_PCT},
                tags=[category], dedup_key=f"counterfactual:{category}",
            )
