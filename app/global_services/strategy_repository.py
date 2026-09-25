import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, SafetyError
from app.models import (
    Hypothesis,
    HypothesisStage,
    StatusHistory,
    Strategy,
    StrategyStatus,
    StrategyVersion,
    SystemEvent,
    ValidationResult,
)


class StrategyRepository:
    allowed_transitions = {
        "draft": {"under_validation", "retired"},
        "under_validation": {"approved", "under_review", "draft", "paper_testing"},
        "approved": {"active", "suspended", "retired"},
        "active": {"suspended", "retired"},
        "suspended": {"active", "under_review", "retired"},
        "under_review": {"under_validation", "suspended", "retired"},
        "paper_testing": {"ready_for_review", "under_review", "suspended", "retired"},
        "ready_for_review": {"approved", "under_review", "retired"},
        "retired": set(),
    }
    # Only a human operator may promote a strategy into live paper decision-making.
    human_only = {"approved", "active"}

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, data: dict) -> Strategy:
        version_data = data.pop("version")
        strategy = Strategy(**data)
        self.session.add(strategy)
        self.session.flush()
        self.add_version(strategy.id, version_data)
        self.session.commit()
        self.session.refresh(strategy)
        return strategy

    def add_version(self, strategy_id: str, data: dict) -> StrategyVersion:
        strategy = self.session.get(Strategy, strategy_id)
        if not strategy:
            raise NotFoundError("Strategy not found")
        if strategy.status in {StrategyStatus.ACTIVE, StrategyStatus.PAPER_TESTING, StrategyStatus.READY_FOR_REVIEW}:
            raise SafetyError("Active versions are immutable; suspend the strategy before versioning")
        versions = self.session.scalars(
            select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        ).all()
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
        version = StrategyVersion(
            strategy_id=strategy_id,
            version=max((v.version for v in versions), default=0) + 1,
            content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            **data,
        )
        self.session.add(version)
        self.session.commit()
        self.session.refresh(version)
        return version

    def transition(self, strategy_id: str, target: str, reason: str, actor: str = "operator") -> Strategy:
        strategy = self.session.get(Strategy, strategy_id)
        if not strategy:
            raise NotFoundError("Strategy not found")
        if target not in self.allowed_transitions.get(strategy.status, set()):
            raise SafetyError(f"Invalid strategy transition: {strategy.status} -> {target}")
        if target in self.human_only and actor != "operator":
            raise SafetyError("Automated research may not approve or activate strategies")
        latest = max(strategy.versions, key=lambda item: item.version)
        if target in {"approved", "active", "paper_testing"}:
            passed = self.session.scalar(
                select(ValidationResult).where(
                    ValidationResult.strategy_version_id == latest.id,
                    ValidationResult.result == "PASS",
                )
            )
            if not passed:
                raise SafetyError("A PASS validation is required before approval or activation")
        if target == "ready_for_review":
            paper = self.session.scalar(select(ValidationResult).where(
                ValidationResult.strategy_version_id == latest.id,
                ValidationResult.method == "paper_validation",
                ValidationResult.result == "PASS",
            ))
            if not paper:
                raise SafetyError("Paper validation PASS is required before review")
        previous = strategy.status
        strategy.status = target
        self.session.add(StatusHistory(strategy_id=strategy.id, from_status=previous, to_status=target, reason=reason))
        event_name = {
            "approved": "StrategyApproved",
            "active": "StrategyActivated",
            "suspended": "StrategySuspended",
            "retired": "StrategyRetired",
            "paper_testing": "StrategyPaperTestingStarted",
            "ready_for_review": "StrategyReadyForReview",
        }.get(target, "StrategyStatusChanged")
        if strategy.hypothesis_id:
            hypothesis = self.session.get(Hypothesis, strategy.hypothesis_id)
            if hypothesis and target == "active":
                hypothesis.stage = HypothesisStage.ACTIVE
                hypothesis.stage_history = [*(hypothesis.stage_history or []), {
                    "stage": "ACTIVE", "reason": f"operator activation: {reason}"}]
            elif hypothesis and target == "retired":
                hypothesis.stage = HypothesisStage.ARCHIVED
        self.session.add(SystemEvent(
            type=event_name,
            component="strategy_repository",
            severity="INFO",
            message=f"Strategy transitioned {previous} -> {target}",
            payload={"strategy_id": strategy.id, "from": previous, "to": target, "reason": reason, "actor": actor},
            correlation_id=strategy.id,
        ))
        self.session.commit()
        self.session.refresh(strategy)
        return strategy
