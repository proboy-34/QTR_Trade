"""Paper eligibility of a strategy version — checked independently by Decision and by Risk.

A strategy is paper eligible only if:
- its repository status allows paper decisions (research `paper_testing`, or operator
  `approved`/`active`), and it is not a demo strategy outside DEMO_MODE;
- its latest version carries PASS evidence for every required research method
  (in-sample backtest, out-of-sample, walk-forward, robustness incl. regime checks).

A single profitable backtest is never sufficient.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Strategy, StrategyVersion, ValidationResult

PAPER_STATUSES = ("paper_testing", "approved", "active")
REQUIRED_METHODS = ("backtest", "out_of_sample", "walk_forward", "robustness")

# Explicit lifecycle vocabulary shown to operators (repository status -> lifecycle status).
LIFECYCLE = {
    "draft": "candidate", "under_validation": "candidate", "under_review": "candidate",
    "paper_testing": "paper_eligible", "ready_for_review": "validated", "approved": "paper_eligible",
    "active": "active", "suspended": "disabled", "retired": "disabled",
}


@dataclass
class Eligibility:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    lifecycle_status: str = "candidate"
    passed_methods: list[str] = field(default_factory=list)


def check(session: Session, settings: Settings, version: StrategyVersion | None) -> Eligibility:
    if version is None:
        return Eligibility(False, ["STRATEGY_VERSION_NOT_FOUND"], "rejected")
    strategy: Strategy | None = version.strategy
    if strategy is None:
        return Eligibility(False, ["STRATEGY_NOT_FOUND"], "rejected")
    latest = max(strategy.versions, key=lambda item: item.version)
    lifecycle = LIFECYCLE.get(strategy.status, "candidate")
    reasons: list[str] = []
    if strategy.status not in PAPER_STATUSES:
        reasons.append(f"STATUS_{strategy.status.upper()}_NOT_PAPER_ELIGIBLE")
    if latest.id != version.id:
        reasons.append("NOT_LATEST_VERSION")
    if strategy.is_demo and not settings.demo_mode:
        reasons.append("DEMO_STRATEGY_OUTSIDE_DEMO_MODE")
    passed = set(session.scalars(select(ValidationResult.method).where(
        ValidationResult.strategy_version_id == version.id, ValidationResult.result == "PASS")).all())
    failed = set(session.scalars(select(ValidationResult.method).where(
        ValidationResult.strategy_version_id == version.id, ValidationResult.result == "FAIL")).all())
    missing = [method for method in REQUIRED_METHODS if method not in passed]
    if missing and not (strategy.is_demo and settings.demo_mode):
        reasons.append("MISSING_VALIDATION:" + ",".join(missing))
    if failed & set(REQUIRED_METHODS):
        reasons.append("FAILED_VALIDATION:" + ",".join(sorted(failed & set(REQUIRED_METHODS))))
    return Eligibility(not reasons, reasons, lifecycle if not reasons else ("rejected" if failed else lifecycle),
                       sorted(passed))
