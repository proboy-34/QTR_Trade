import numpy as np
import pandas as pd

from app.global_services.validation import WalkForwardConfig, WalkForwardValidator
from app.research.analysis import ResearchAnalyzer
from app.research.experiments import ExperimentManager
from app.research.review import ResearchReviewService


def candles(rows: int = 260) -> pd.DataFrame:
    close = 100 + np.sin(np.arange(rows) / 7) * 9 + np.linspace(0, 20, rows)
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=rows, freq="h", tz="UTC"),
        "open": close,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 1000 + np.arange(rows),
    })


def test_research_analysis_returns_statistics_patterns_and_causality_warning():
    result = ResearchAnalyzer().analyze(candles())
    assert result["statistics"]["standard_deviation"] > 0
    assert "breakouts" in result["patterns"]
    assert any("not evidence of causality" in item for item in result["insights"])


def test_research_review_catches_missing_and_lookahead_rules():
    result = ResearchReviewService().review({
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "entry_rules": [{"value": "next_close"}],
        "exit_rules": [],
        "parameters": {"fast": 10},
        "documentation": "short",
    })
    assert result.status == "FAIL"
    assert "POTENTIAL_LOOK_AHEAD_BIAS" in result.errors
    assert "MISSING_EXIT_RULES" in result.errors


def test_walk_forward_produces_rolling_oos_windows():
    result = WalkForwardValidator().run(
        candles(), WalkForwardConfig(train_size=100, validation_size=50, step_size=50), fast=5, slow=20
    )
    assert result["aggregate"]["windows"] == 3
    assert all("out_of_sample" in window for window in result["windows"])


def test_experiment_duplicate_prevention_and_lifecycle(session):
    manager = ExperimentManager(session)
    first = manager.create("A", {"symbol": "BTCUSDT", "fast": 5})
    duplicate = manager.create("B", {"fast": 5, "symbol": "BTCUSDT"})
    assert first.id == duplicate.id
    completed = manager.execute(first, lambda config: {"tested": config["symbol"]})
    assert completed.status == "COMPLETED" and completed.progress == 100
    assert [entry["event"] for entry in completed.logs] == ["QUEUED", "RUNNING", "COMPLETED"]


def test_failed_experiment_is_preserved_with_error(session):
    experiment = ExperimentManager(session).create("Failure", {"case": "failure"})

    def fail(_: dict) -> dict:
        raise RuntimeError("controlled failure")

    ExperimentManager(session).execute(experiment, fail)
    assert experiment.status == "FAILED"
    assert "controlled failure" in experiment.error

