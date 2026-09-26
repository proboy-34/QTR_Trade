"""Deterministic post-trade analyst. Quantitative facts come only from QTR's own records."""

from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.memory.knowledge import KnowledgeBase
from app.models import (
    Decision,
    MarketEvent,
    PostTradeAnalysis,
    TradeMemory,
    ValidationResult,
)


class PostTradeAnalyst:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.knowledge = KnowledgeBase(session)

    def analyze(self, trade: TradeMemory) -> PostTradeAnalysis:
        existing = self.session.scalar(select(PostTradeAnalysis).where(PostTradeAnalysis.trade_memory_id == trade.id))
        if existing:
            return existing
        entry = float(trade.entry_price)
        stop_pct = abs(entry - float(trade.stop_loss)) / entry * 100 if entry else 0.0
        target_pct = abs(float(trade.take_profit) - entry) / entry * 100 if entry else 0.0
        decision = self.session.get(Decision, trade.decision_id) if trade.decision_id else None
        validation = self.session.scalar(select(ValidationResult).where(
            ValidationResult.strategy_version_id == trade.strategy_version_id,
            ValidationResult.method.in_(["out_of_sample", "backtest"]),
        ).order_by(desc(ValidationResult.created_at)))
        expected = {
            "stop_pct": round(stop_pct, 4), "target_pct": round(target_pct, 4),
            "reward_to_risk": round(target_pct / stop_pct, 3) if stop_pct else None,
            "decision_confidence": decision.confidence if decision else None,
            "research_expectancy_pct": (validation.metrics or {}).get("expectancy_pct") if validation else None,
        }
        rationale = (decision.rationale or {}) if decision else {}
        expected.update({
            "planned": {"entry": str(trade.entry_price), "stop_loss": str(trade.stop_loss), "take_profit": str(trade.take_profit)},
            "strategy": rationale.get("strategy"), "regime": rationale.get("market_regime"),
            "thesis": rationale.get("thesis"), "invalidation": rationale.get("invalidation"),
            "ai_decision": {key: (rationale.get("ai_review") or {}).get(key) for key in (
                "status", "decision", "confidence", "thesis", "invalidation", "reasons", "artifact_id")},
            "risk_decision": rationale.get("risk"), "market_conditions": rationale.get("candle"),
        })
        mfe, mae = trade.mfe_pct or 0.0, trade.mae_pct or 0.0
        actual = {
            "exit_reason": trade.exit_reason, "return_pct": round(trade.return_pct, 4),
            "r_multiple": round(trade.r_multiple, 3) if trade.r_multiple is not None else None,
            "mfe_pct": round(mfe, 4), "mae_pct": round(mae, 4), "holding_hours": round(trade.holding_seconds / 3600, 3),
            "net_pnl": str(trade.net_pnl), "fees": str(trade.fees), "slippage_cost": str(trade.slippage_cost),
            "exit_price": str(trade.exit_price) if getattr(trade, "exit_price", None) is not None else None,
            "execution_quality": {"slippage_pct_of_notional": round(float(trade.slippage_cost) / (float(trade.entry_price) * float(trade.quantity)) * 100, 5)
                                  if float(trade.entry_price) * float(trade.quantity) else None,
                                  "fills": "SIMULATED against real Binance quotes (paper)"},
        }
        thesis: bool | None
        if trade.exit_reason == "take_profit" or (stop_pct and mfe >= stop_pct):
            thesis = True
        elif str(trade.exit_reason).startswith("stop_loss") and mfe < stop_pct * 0.5:
            thesis = False
        else:
            thesis = None
        if stop_pct and abs(min(mae, 0)) < stop_pct * 0.5:
            entry_quality = "GOOD"
        elif str(trade.exit_reason).startswith("stop_loss") and mfe < stop_pct * 0.25:
            entry_quality = "POOR_IMMEDIATELY_ADVERSE"
        else:
            entry_quality = "AVERAGE"
        capture = trade.return_pct / mfe if mfe > 0 else None
        if stop_pct and mfe >= stop_pct and trade.return_pct <= 0:
            exit_quality = "LEFT_PROFIT"
        elif capture is not None and capture >= 0.5:
            exit_quality = "GOOD"
        else:
            exit_quality = "AVERAGE"
        factors = self._factors(trade, stop_pct)
        outcome = "win" if float(trade.net_pnl) > 0 else "loss"
        lesson = (
            f"{trade.symbol} {trade.side} {outcome} ({trade.return_pct:.2f}%) exited by {trade.exit_reason}; "
            f"thesis {'confirmed' if thesis else 'refuted' if thesis is False else 'inconclusive'}; "
            f"entry {entry_quality.lower()}, exit {exit_quality.lower()}; regime at entry "
            f"{trade.regime_at_entry or 'unknown'}" + (f"; factors: {', '.join(factors)}" if factors else "")
        )
        entry_knowledge = self.knowledge.record(
            "TRADE_LESSON", lesson[:300], body=lesson,
            evidence={"expected": expected, "actual": actual, "factors": factors},
            refs={"trade_memory_id": trade.id, "position_id": trade.position_id, "decision_id": trade.decision_id},
            tags=[outcome, trade.exit_reason, entry_quality, exit_quality], symbol=trade.symbol,
            timeframe=trade.timeframe, regime=trade.regime_at_entry, dedup_key=f"lesson:{trade.id}",
        )
        self.knowledge.record(
            "STRATEGY_BEHAVIOR",
            f"Strategy version {trade.strategy_version_id[:8]} {outcome}s in {trade.regime_at_entry or 'UNKNOWN'}",
            body="Recurring outcome counter by strategy version, regime at entry and result.",
            evidence={"last_trade": trade.id, "return_pct": trade.return_pct},
            refs={"strategy_version_id": trade.strategy_version_id}, tags=[outcome],
            symbol=trade.symbol, timeframe=trade.timeframe, regime=trade.regime_at_entry,
            dedup_key=f"behavior:{trade.strategy_version_id}:{trade.regime_at_entry}:{outcome}",
        )
        analysis = PostTradeAnalysis(
            trade_memory_id=trade.id, expected=expected, actual=actual, thesis_correct=thesis,
            entry_quality=entry_quality, exit_quality=exit_quality, factors=factors, lesson=lesson,
            knowledge_entry_id=entry_knowledge.id,
        )
        self.session.add(analysis)
        self.session.flush()
        return analysis

    def _factors(self, trade: TradeMemory, stop_pct: float) -> list[str]:
        factors: list[str] = []
        if trade.regime_at_entry and trade.regime_at_exit and trade.regime_at_entry != trade.regime_at_exit:
            factors.append("REGIME_CHANGED_DURING_TRADE")
        if trade.event_ids:
            events = self.session.scalars(select(MarketEvent).where(MarketEvent.id.in_(trade.event_ids))).all()
            if any(event.severity in {"HIGH", "CRITICAL"} for event in events):
                factors.append("HIGH_IMPACT_EVENT_DURING_TRADE")
        notional = float(trade.entry_price) * float(trade.quantity)
        if notional and float(trade.slippage_cost) / notional > 0.001:
            factors.append("ADVERSE_ENTRY_SLIPPAGE")
        if str(trade.exit_reason).startswith("stop_loss") and trade.return_pct < -(stop_pct * 1.1):
            factors.append("GAP_THROUGH_STOP")
        gross = float(trade.realized_pnl)
        if gross > 0 and float(trade.net_pnl) <= 0:
            factors.append("FEES_EXCEEDED_GROSS_PROFIT")
        return factors


def learning_summary(session: Session) -> dict[str, Any]:
    from sqlalchemy import func

    analyses = session.scalar(select(func.count()).select_from(PostTradeAnalysis)) or 0
    thesis = dict(session.execute(select(PostTradeAnalysis.thesis_correct, func.count()).group_by(PostTradeAnalysis.thesis_correct)).all())
    return {"trades_analyzed": analyses,
            "thesis_confirmed": thesis.get(True, 0), "thesis_refuted": thesis.get(False, 0),
            "thesis_inconclusive": thesis.get(None, 0)}
