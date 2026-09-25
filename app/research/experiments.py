import hashlib
import json
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import TimeService
from app.models import Experiment


class ExperimentManager:
    transitions = {
        "QUEUED": {"RUNNING", "CANCELLED"},
        "RUNNING": {"PAUSED", "COMPLETED", "FAILED", "CANCELLED"},
        "PAUSED": {"RUNNING", "CANCELLED"},
        "COMPLETED": set(),
        "FAILED": {"QUEUED"},
        "CANCELLED": set(),
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def fingerprint(configuration: dict) -> str:
        return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()

    def create(self, name: str, configuration: dict, research_plan_id: str | None = None) -> Experiment:
        fingerprint = self.fingerprint(configuration)
        existing = self.session.scalar(select(Experiment).where(Experiment.fingerprint == fingerprint))
        if existing:
            return existing
        experiment = Experiment(name=name, configuration=configuration, fingerprint=fingerprint, research_plan_id=research_plan_id)
        experiment.logs = [{"at": TimeService.now().isoformat(), "event": "QUEUED"}]
        self.session.add(experiment)
        self.session.commit()
        return experiment

    def transition(self, experiment: Experiment, target: str) -> Experiment:
        if target not in self.transitions[experiment.status]:
            raise ValueError(f"Invalid experiment transition {experiment.status} -> {target}")
        experiment.status = target
        experiment.logs = [*experiment.logs, {"at": TimeService.now().isoformat(), "event": target}]
        self.session.commit()
        return experiment

    def execute(self, experiment: Experiment, runner: Callable[[dict], dict]) -> Experiment:
        self.transition(experiment, "RUNNING")
        experiment.started_at = TimeService.now()
        experiment.progress = 10
        self.session.commit()
        try:
            experiment.result = runner(experiment.configuration)
            experiment.progress = 100
            experiment.finished_at = TimeService.now()
            experiment.reproducibility = {
                "fingerprint": experiment.fingerprint,
                "configuration": experiment.configuration,
                "dataset_id": experiment.dataset_id,
                "strategy_version_id": experiment.strategy_version_id,
            }
            self.transition(experiment, "COMPLETED")
        except Exception as exc:
            experiment.error = f"{type(exc).__name__}: {exc}"
            experiment.finished_at = TimeService.now()
            self.transition(experiment, "FAILED")
        if experiment.started_at and experiment.finished_at:
            experiment.runtime_ms = max(
                0, round((experiment.finished_at - experiment.started_at).total_seconds() * 1000)
            )
            self.session.commit()
        return experiment
