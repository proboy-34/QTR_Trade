"""Deterministic research generation from accumulated evidence (works without any AI).

Scanner signals and learning findings are mapped to reviewed specification templates.
Every generated idea is an ordinary hypothesis and must pass every validation gate.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import EventBus
from app.core.lineage import fingerprint
from app.models import Hypothesis, Opportunity
from app.research.hypotheses import HypothesisEngine

TEMPLATES: dict[str, dict[str, Any]] = {
    "BREAKOUT_UP": {
        "statement": "Volume-confirmed 20-bar breakouts in {symbol} continue over the following bars",
        "spec": {
            "entry": [{"left": "close", "operator": "gt", "right": "highest:{lookback}"},
                      {"left": "volume_ratio:20", "operator": "gt", "right": 1.5}],
            "exit": [{"left": "close", "operator": "lt", "right": "ema:{exit_ema}"}],
            "risk": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06},
            "parameters": {"lookback": 20, "exit_ema": 20},
        },
    },
    "TREND_CHANGE_UP": {
        "statement": "EMA trend reversals to the upside in {symbol} have positive follow-through",
        "spec": {
            "entry": [{"left": "ema:{fast}", "operator": "crosses_above", "right": "ema:{slow}"}],
            "exit": [{"left": "ema:{fast}", "operator": "crosses_below", "right": "ema:{slow}"}],
            "risk": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06},
            "parameters": {"fast": 12, "slow": 36},
        },
    },
    "VOLATILITY_EXPANSION": {
        "statement": "After volatility expansion with positive momentum, {symbol} trends persist",
        "spec": {
            "entry": [{"left": "return:10", "operator": "gt", "right": 0},
                      {"left": "efficiency:20", "operator": "gt", "right": 0.35},
                      {"left": "close", "operator": "gt", "right": "ema:{trend}"}],
            "exit": [{"left": "close", "operator": "lt", "right": "ema:{trend}"}],
            "risk": {"stop_loss_pct": 0.04, "take_profit_pct": 0.08},
            "parameters": {"trend": 50},
        },
    },
}


# A volume spike is researched as the volume-confirmed breakout it may precede.
TEMPLATES["VOLUME_SPIKE"] = TEMPLATES["BREAKOUT_UP"]


class ResearchGenerator:
    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus

    def _known(self) -> set[str]:
        specs: list[dict[str, Any]] = list(self.session.scalars(select(Hypothesis.spec)).all())
        return {fingerprint(spec) for spec in specs if spec}

    def from_opportunities(self, limit: int = 2) -> list[str]:
        known = self._known()
        created: list[str] = []
        opportunities = self.session.scalars(select(Opportunity).where(
            Opportunity.source == "scanner", Opportunity.status.in_(["DETECTED", "QUEUED"]),
        ).order_by(Opportunity.rank_score.desc()).limit(50)).all()
        engine = HypothesisEngine(self.session, self.settings, self.event_bus)
        for opportunity in opportunities:
            for signal in opportunity.signals or []:
                template = TEMPLATES.get(signal.get("code", ""))
                if not template:
                    continue
                spec: dict[str, Any] = {**template["spec"], "universe": [opportunity.symbol], "timeframes": [opportunity.timeframe or "1h"]}
                key = fingerprint(spec)
                if key in known:
                    continue
                hypothesis = engine.create(
                    template["statement"].format(symbol=opportunity.symbol), origin="scanner", spec=spec,
                    description=f"Generated from scanner signal {signal.get('code')} on opportunity {opportunity.id}.",
                    market_conditions={"signal": signal, "regime": opportunity.regime},
                    variables={"exchange": opportunity.exchange, "opportunity_id": opportunity.id},
                    assumptions=["Signal definitions use only closed-candle information",
                                 "Costs follow the configured paper model"],
                )
                known.add(key)
                created.append(hypothesis.id)
                if len(created) >= limit:
                    return created
        return created
