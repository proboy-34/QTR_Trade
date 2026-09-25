"""Strategy challengers: variations researched against an unchanged champion."""

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import NotFoundError, QTRError
from app.core.events import EventBus
from app.models import Hypothesis, Strategy
from app.research.dsl import spec_from_version
from app.research.hypotheses import HypothesisEngine


class ChallengerService:
    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus

    def propose(self, strategy_id: str, *, parameters: dict[str, float] | None = None,
                spec_overrides: dict[str, Any] | None = None, rationale: str = "") -> Hypothesis:
        champion = self.session.get(Strategy, strategy_id)
        if not champion or not champion.versions:
            raise NotFoundError("Champion strategy not found")
        version = max(champion.versions, key=lambda item: item.version)
        spec = spec_from_version(version, champion)
        if spec is None:
            raise QTRError("Champion has no declarative specification; challengers cannot be derived")
        payload = spec.model_dump()
        if parameters:
            unknown = set(parameters) - set(payload["parameters"])
            if unknown:
                raise QTRError(f"Unknown champion parameters: {sorted(unknown)}")
            payload["parameters"] = {**payload["parameters"], **parameters}
        for key in ("entry", "exit", "risk", "regimes"):
            if spec_overrides and key in spec_overrides:
                payload[key] = spec_overrides[key]
        changes = {"parameters": parameters or {}, **{k: v for k, v in (spec_overrides or {}).items() if k in payload}}
        return HypothesisEngine(self.session, self.settings, self.event_bus).create(
            f"Challenger to {champion.name[:120]}: {changes}"[:900], origin="challenger", spec=payload,
            parent_strategy_id=champion.id,
            description=rationale or "Variation of the champion; must beat it out-of-sample with no worse drawdown.",
            variables={"exchange": champion.exchange if champion.exchange != "paper" else None, "changes": changes},
        )
