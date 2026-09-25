"""Strategy decay detection: historical expectation versus recent paper evidence.

Health changes never modify, delete or deactivate a strategy. A degraded strategy is
flagged, an event is published, and a follow-up research hypothesis may be proposed.
"""

from math import sqrt
from typing import Any

import numpy as np
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.memory.knowledge import KnowledgeBase
from app.models import (
    Hypothesis,
    HypothesisStage,
    Strategy,
    StrategyHealthRecord,
    TradeMemory,
    ValidationResult,
)
from app.research.dsl import spec_from_version

MONITORED = ("active", "paper_testing", "ready_for_review", "approved", "suspended")


def _stats(returns: list[float], pnls: list[float]) -> dict[str, Any]:
    values = np.array(returns, dtype=float)
    money = np.array(pnls, dtype=float)
    wins, losses = money[money > 0].sum(), -money[money < 0].sum()
    std = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    return {
        "trades": len(values),
        "expectancy_pct": round(float(values.mean()), 5) if len(values) else 0.0,
        "std_pct": round(std, 5),
        "sharpe_per_trade": round(float(values.mean()) / std, 4) if std > 0 else 0.0,
        "profit_factor": round(float(wins / losses), 4) if losses else (999.0 if wins else 0.0),
        "win_rate": round(float((money > 0).mean() * 100), 2) if len(money) else 0.0,
    }


class StrategyHealthService:
    def __init__(self, session: Session, settings: Settings, event_bus: EventBus,
                 recent_window: int = 20, minimum_trades: int = 8) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus
        self.recent_window = recent_window
        self.minimum_trades = minimum_trades

    async def evaluate_all(self) -> list[dict[str, Any]]:
        results = []
        for strategy in self.session.scalars(select(Strategy).where(Strategy.status.in_(MONITORED))).all():
            results.append(await self.evaluate(strategy))
        return results

    async def evaluate(self, strategy: Strategy) -> dict[str, Any]:
        version = max(strategy.versions, key=lambda item: item.version) if strategy.versions else None
        version_ids = [item.id for item in strategy.versions]
        trades = list(self.session.scalars(select(TradeMemory).where(
            TradeMemory.strategy_version_id.in_(version_ids)
        ).order_by(TradeMemory.closed_at)).all()) if version_ids else []
        recent_trades = trades[-self.recent_window:]
        recent = _stats([t.return_pct for t in recent_trades], [float(t.net_pnl) for t in recent_trades])
        validation = None
        if version is not None:
            validation = self.session.scalar(select(ValidationResult).where(
                ValidationResult.strategy_version_id == version.id,
                ValidationResult.method.in_(["out_of_sample", "backtest"]),
            ).order_by(desc(ValidationResult.created_at)))
        if validation and (validation.metrics or {}).get("expectancy_pct") is not None:
            historical = {"source": f"validation:{validation.method}",
                          "expectancy_pct": float(validation.metrics["expectancy_pct"]),
                          "profit_factor": validation.metrics.get("profit_factor")}
        else:
            older = trades[: -self.recent_window]
            historical = {"source": "earlier_paper_trades", **_stats([t.return_pct for t in older], [float(t.net_pnl) for t in older])}
        by_regime: dict[str, Any] = {}
        for regime in {trade.regime_at_entry or "UNKNOWN" for trade in trades}:
            items = [trade for trade in trades if (trade.regime_at_entry or "UNKNOWN") == regime]
            by_regime[regime] = {**_stats([t.return_pct for t in items], [float(t.net_pnl) for t in items]),
                                 "evidence": "sufficient" if len(items) >= 5 else "insufficient"}
        status, reasons = self._classify(recent, historical)
        previous = strategy.health_status
        record = StrategyHealthRecord(strategy_id=strategy.id, strategy_version_id=version.id if version else None,
                                      status=status, historical=historical, recent=recent, by_regime=by_regime, reasons=reasons)
        self.session.add(record)
        strategy.health_status = status
        strategy.health_details = {"reasons": reasons, "recent": recent, "historical": historical, "record_id": None}
        self.session.flush()
        strategy.health_details = {**strategy.health_details, "record_id": record.id}
        follow_up = None
        if status == "DEGRADED" and previous != "DEGRADED":
            await publish_persisted(self.session, self.event_bus, Event("STRATEGY_DEGRADED", {
                "strategy_id": strategy.id, "reasons": reasons, "health_record_id": record.id,
            }, source="strategy_health"), "learning")
            if strategy.hypothesis_id:
                hypothesis = self.session.get(Hypothesis, strategy.hypothesis_id)
                if hypothesis and hypothesis.stage in {HypothesisStage.PAPER_TESTING, HypothesisStage.PAPER_VALIDATED,
                                                       HypothesisStage.CANDIDATE, HypothesisStage.ACTIVE}:
                    hypothesis.stage = HypothesisStage.DEGRADED
                    hypothesis.stage_history = [*(hypothesis.stage_history or []), {"stage": "DEGRADED", "reason": "; ".join(reasons)}]
            follow_up = self._follow_up(strategy, by_regime)
            KnowledgeBase(self.session).record(
                "STRATEGY_BEHAVIOR", f"{strategy.name[:200]} degraded", body="; ".join(reasons),
                evidence={"recent": recent, "historical": historical, "by_regime": by_regime},
                refs={"strategy_id": strategy.id, "health_record_id": record.id, "follow_up_hypothesis_id": follow_up},
                tags=["degradation"], symbol=strategy.symbol, timeframe=strategy.timeframe,
            )
        self.session.commit()
        return {"strategy_id": strategy.id, "status": status, "reasons": reasons, "follow_up_hypothesis_id": follow_up}

    def _classify(self, recent: dict[str, Any], historical: dict[str, Any]) -> tuple[str, list[str]]:
        if recent["trades"] < self.minimum_trades:
            return "INSUFFICIENT_DATA", [f"{recent['trades']} recent trades; {self.minimum_trades} required"]
        expected = float(historical.get("expectancy_pct") or 0.0)
        reasons: list[str] = []
        t_stat = None
        if recent["std_pct"] > 0:
            t_stat = (recent["expectancy_pct"] - expected) / (recent["std_pct"] / sqrt(recent["trades"]))
        if expected > 0 and recent["expectancy_pct"] < 0 and recent["profit_factor"] < 0.8 and (t_stat is None or t_stat < -2):
            reasons.append(f"recent expectancy {recent['expectancy_pct']}% vs historical {expected}% (t={t_stat and round(t_stat, 2)})")
            return "DEGRADED", reasons
        if recent["profit_factor"] < 1 or (expected > 0 and recent["expectancy_pct"] < expected * 0.5):
            reasons.append(f"recent profit factor {recent['profit_factor']}, expectancy {recent['expectancy_pct']}% vs {expected}%")
            return "REVIEW_REQUIRED", reasons
        return "HEALTHY", ["recent performance consistent with expectation"]

    def _follow_up(self, strategy: Strategy, by_regime: dict[str, Any]) -> str | None:
        """Propose research restricting the strategy to regimes where it historically worked."""
        from app.research.hypotheses import HypothesisEngine

        existing = self.session.scalar(select(Hypothesis.id).where(
            Hypothesis.parent_strategy_id == strategy.id, Hypothesis.origin == "strategy_health",
            Hypothesis.stage.notin_([HypothesisStage.REJECTED, HypothesisStage.FAILED, HypothesisStage.ARCHIVED]),
        ))
        if existing or not strategy.versions:
            return None
        good = [regime for regime, stats in by_regime.items()
                if stats["evidence"] == "sufficient" and stats["profit_factor"] >= 1.2 and regime != "UNKNOWN"]
        spec = spec_from_version(max(strategy.versions, key=lambda item: item.version), strategy)
        if spec is None or not good:
            return None
        restricted = spec.model_copy(update={"regimes": sorted(good)})
        hypothesis = HypothesisEngine(self.session, self.settings, self.event_bus).create(
            f"{strategy.name[:100]} restricted to {', '.join(sorted(good))}",
            origin="strategy_health", spec=restricted.model_dump(), parent_strategy_id=strategy.id,
            description="Generated after degradation: test whether the edge survives only in regimes with positive paper evidence.",
            assumptions=["Regime labels at entry are informative", "Recent degradation is regime-driven"],
        )
        return hypothesis.id
