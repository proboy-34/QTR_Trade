import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, SafetyError
from app.models import (
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
        "under_validation": {"approved", "under_review", "draft"},
        "approved": {"active", "suspended", "retired"},
        "active": {"suspended", "retired"},
        "suspended": {"active", "under_review", "retired"},
        "under_review": {"under_validation", "suspended", "retired"},
        "retired": set(),
    }

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
        if strategy.status == StrategyStatus.ACTIVE:
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

    def transition(self, strategy_id: str, target: str, reason: str) -> Strategy:
        strategy = self.session.get(Strategy, strategy_id)
        if not strategy:
            raise NotFoundError("Strategy not found")
        if target not in self.allowed_transitions[strategy.status]:
            raise SafetyError(f"Invalid strategy transition: {strategy.status} -> {target}")
        if target in {"approved", "active"}:
            latest = max(strategy.versions, key=lambda item: item.version)
            passed = self.session.scalar(
                select(ValidationResult).where(
                    ValidationResult.strategy_version_id == latest.id,
                    ValidationResult.result == "PASS",
                )
            )
            if not passed:
                raise SafetyError("A PASS validation is required before approval or activation")
        previous = strategy.status
        strategy.status = target
        self.session.add(StatusHistory(strategy_id=strategy.id, from_status=previous, to_status=target, reason=reason))
        event_name = {
            "approved": "StrategyApproved",
            "active": "StrategyActivated",
            "suspended": "StrategySuspended",
            "retired": "StrategyRetired",
        }.get(target, "StrategyStatusChanged")
        self.session.add(SystemEvent(
            type=event_name,
            component="strategy_repository",
            severity="INFO",
            message=f"Strategy transitioned {previous} -> {target}",
            payload={"strategy_id": strategy.id, "from": previous, "to": target, "reason": reason},
            correlation_id=strategy.id,
        ))
        self.session.commit()
        self.session.refresh(strategy)
        return strategy
