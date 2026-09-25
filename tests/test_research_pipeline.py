import numpy as np
import pandas as pd
import pytest
from market_fixtures import EMA_SPEC, oscillating, random_walk, store
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.global_services.strategy_repository import StrategyRepository
from app.models import (
    Experiment,
    Hypothesis,
    KnowledgeEntry,
    Strategy,
    SystemEvent,
    ValidationResult,
)
from app.research.dsl import CompiledStrategy, parse_spec, spec_from_version, validation_errors
from app.research.hypotheses import HypothesisEngine, research_counts
from app.research.robustness import deflated_sharpe, monte_carlo, passive_benchmark, perturbations
from app.research.spec_backtest import CostModel, SpecBacktestEngine


def test_dsl_rejects_code_unknown_features_and_bad_scope():
    assert validation_errors(EMA_SPEC) == []
    bad = [
        {**EMA_SPEC, "entry": [{"left": "__import__('os')", "operator": "gt", "right": 1}]},
        {**EMA_SPEC, "entry": [{"left": "close", "operator": "exec", "right": 1}]},
        {**EMA_SPEC, "entry": [{"left": "ema:{missing}", "operator": "gt", "right": 1}]},
        {**EMA_SPEC, "timeframes": ["2h"]},
        {**EMA_SPEC, "universe": ["../etc"]},
        {**EMA_SPEC, "entry": [{"left": "ema:5000", "operator": "gt", "right": 1}]},
        {**EMA_SPEC, "side": "short"},
    ]
    for payload in bad:
        assert validation_errors(payload), payload
    assert parse_spec(EMA_SPEC).parameter_count == 2


def test_legacy_repository_rules_are_declarative():
    class Version:
        entry_rules = [{"left": "ema_fast", "operator": "crosses_above", "right": "ema_slow"}]
        exit_rules = [{"left": "ema_fast", "operator": "crosses_below", "right": "ema_slow"}]
        parameters = {"fast": 10, "slow": 30}
        filters = {"regime": ["trending"]}
        risk_assumptions = {"risk_fraction": 0.01, "stop_loss_pct": 0.02}

    class StrategyStub:
        symbol, timeframe = "BTCUSDT", "1h"

    spec = spec_from_version(Version(), StrategyStub())
    assert spec is not None and spec.universe == ["BTCUSDT"] and spec.risk.stop_loss_pct == 0.02
    Version.entry_rules = [{"rule": "bullish"}]
    assert spec_from_version(Version(), StrategyStub()) is None


def test_signals_do_not_look_ahead_and_last_bar_signal_is_never_filled():
    frame = oscillating(300)
    spec = parse_spec(EMA_SPEC)
    full = CompiledStrategy(spec).signals(frame)
    partial = CompiledStrategy(spec).signals(frame.iloc[:200])
    assert full.iloc[:200].equals(partial)
    last_entry = int(np.flatnonzero(full["entry"].to_numpy())[-1])
    truncated = frame.iloc[: last_entry + 1]
    result = SpecBacktestEngine().run(spec, truncated, symbol="SOLUSDT")
    assert all(trade["entry_index"] <= last_entry for trade in result["trades"])
    assert all(trade["entry_index"] - 1 != last_entry for trade in result["trades"])


def test_stop_is_assumed_before_target_and_costs_reduce_returns():
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC"),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000.0,
    })
    frame.loc[frame.index[-2], ["close"]] = 101.0  # entry signal at bar -2
    frame.loc[frame.index[-1], ["open", "high", "low", "close"]] = [101.0, 120.0, 80.0, 100.0]  # both touched
    spec = parse_spec({**EMA_SPEC, "entry": [{"left": "close", "operator": "gt", "right": 100.5}], "exit": [],
                       "risk": {"stop_loss_pct": 0.05, "take_profit_pct": 0.05}})
    result = SpecBacktestEngine().run(spec, frame, symbol="SOLUSDT", costs=CostModel(0, 0, 0))
    assert result["trades"][0]["reason"] == "stop_loss"
    free = SpecBacktestEngine().run(parse_spec(EMA_SPEC), oscillating(600), costs=CostModel(0, 0, 0))
    costly = SpecBacktestEngine().run(parse_spec(EMA_SPEC), oscillating(600), costs=CostModel(0.002, 0.002, 20))
    assert costly["metrics"]["total_return"] < free["metrics"]["total_return"]
    assert costly["metrics"]["fees_paid"] > 0 and "regimes" in costly


def test_overfitting_statistics():
    few = deflated_sharpe(0.05, 1000, trials=1, trial_sharpe_variance=0.001)
    many = deflated_sharpe(0.05, 1000, trials=500, trial_sharpe_variance=0.001)
    assert many["benchmark_sharpe"] > few["benchmark_sharpe"] and many["deflated_sharpe"] < few["deflated_sharpe"]
    simulation = monte_carlo([1.0, -0.5, 2.0, -1.0, 0.5, 1.5, -0.2])
    assert simulation["return_p5"] <= simulation["return_p50"] <= simulation["return_p95"]
    assert monte_carlo([1.0, 2.0])["simulations"] == 0
    variants = perturbations(parse_spec(EMA_SPEC))
    assert {"fast": 3, "slow": 15} in variants and {"fast": 7, "slow": 15} in variants and len(variants) == 8
    benchmark = passive_benchmark(pd.DataFrame({"close": [100.0, 110.0, 120.0]}), 0, {"exposure": 0.5, "total_return": 5})
    assert benchmark["exposure_adjusted_benchmark_pct"] == 10.0 and benchmark["excess_return_pct"] == -5.0


@pytest.mark.asyncio
async def test_hypothesis_passes_every_gate_and_enters_paper_testing_only(session):
    store(session, oscillating(1600), "SOLUSDT")
    engine = HypothesisEngine(session, Settings(), EventBus())
    hypothesis = engine.create("EMA crossover captures SOL swings", spec=EMA_SPEC, variables={"exchange": "binance"})
    await engine.run_until_paper(hypothesis)
    history = [item["stage"] for item in hypothesis.stage_history]
    assert history == ["IDEA", "RESEARCH", "BACKTESTING", "OOS_VALIDATION", "WALK_FORWARD", "ROBUSTNESS", "PAPER_TESTING"]
    strategy = session.get(Strategy, hypothesis.strategy_id)
    assert strategy.status == "paper_testing" and strategy.hypothesis_id == hypothesis.id and strategy.origin == "operator"
    methods = {item.method: item.result for item in session.scalars(select(ValidationResult).where(
        ValidationResult.strategy_version_id == strategy.versions[0].id)).all()}
    assert methods == {"backtest": "PASS", "out_of_sample": "PASS", "walk_forward": "PASS", "robustness": "PASS"}
    robustness = hypothesis.evidence["robustness"]["summary"]
    assert robustness["trials"] >= 1 and robustness["parameter_count"] == 2 and robustness["deflated_sharpe"] >= 0.95
    assert hypothesis.evidence["out_of_sample"]["summary"]["excess_over_passive_pct"] > 0
    assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "RESEARCH_CANDIDATE_CREATED")) == 1
    assert session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.kind == "FINDING")) is not None
    # Research can never activate a strategy; only a human operator may.
    with pytest.raises(SafetyError):
        StrategyRepository(session).transition(strategy.id, "approved", "auto", actor="research")
    counts = research_counts(session)
    assert counts["paper_testing"] == 1 and counts["experiments"] == len(hypothesis.evidence["experiment_ids"])


@pytest.mark.asyncio
async def test_experiments_are_reproducible_and_deduplicated(session):
    store(session, oscillating(1600), "SOLUSDT")
    engine = HypothesisEngine(session, Settings(), EventBus())
    first = engine.create("first", spec=EMA_SPEC, variables={"exchange": "binance"})
    await engine.advance(first)
    await engine.advance(first)
    experiment = session.get(Experiment, first.evidence["experiment_ids"][0])
    assert experiment.status == "COMPLETED" and experiment.reproducibility["fingerprint"] == experiment.fingerprint
    assert experiment.configuration["dataset_fingerprint"] == first.evidence["scope"][0]["fingerprint"]
    rerun = engine._experiment(first, "BACKTESTING", first.evidence["scope"][0], lambda _c: {"metrics": {}})
    assert rerun == experiment.result  # identical configuration reuses the recorded result


@pytest.mark.asyncio
async def test_random_walk_idea_is_rejected_and_remembered(session):
    store(session, random_walk(1600, seed=0), "SOLUSDT")
    engine = HypothesisEngine(session, Settings(), EventBus())
    hypothesis = engine.create("EMA crossover on noise", spec=EMA_SPEC, variables={"exchange": "binance"})
    await engine.run_until_paper(hypothesis)
    assert hypothesis.stage == "FAILED" and hypothesis.decision_reason.endswith("_FAILED")
    assert hypothesis.strategy_id is None and session.scalar(select(func.count()).select_from(Strategy)) == 0
    failed = session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.kind == "FAILED_IDEA"))
    assert failed is not None and failed.refs["hypothesis_id"] == hypothesis.id
    assert research_counts(session)["rejected"] == 1


@pytest.mark.asyncio
async def test_insufficient_data_and_invalid_specs_are_rejected_with_reasons(session):
    engine = HypothesisEngine(session, Settings(), EventBus())
    no_data = engine.create("no data", spec=EMA_SPEC)
    await engine.advance(no_data)
    assert no_data.stage == "REJECTED" and no_data.decision_reason == "INSUFFICIENT_DATA"
    invalid = engine.create("invalid", spec={"entry": []})
    assert invalid.stage == "REJECTED" and invalid.decision_reason == "INVALID_SPEC"
    legacy = Hypothesis(statement="legacy without spec")
    session.add(legacy)
    session.commit()
    outcome = await engine.advance(legacy)
    assert not outcome["changed"] and outcome["reason"].startswith("NO_SPECIFICATION")


@pytest.mark.asyncio
async def test_challenger_must_beat_unchanged_champion(session):
    from app.research.challengers import ChallengerService

    store(session, oscillating(1600), "SOLUSDT")
    engine = HypothesisEngine(session, Settings(), EventBus())
    weaker = {**EMA_SPEC, "parameters": {"fast": 3, "slow": 40}}
    champion_hypothesis = engine.create("champion", spec=weaker, variables={"exchange": "binance"})
    await engine.run_until_paper(champion_hypothesis)
    champion = session.get(Strategy, champion_hypothesis.strategy_id)
    champion_hash = champion.versions[0].content_hash
    service = ChallengerService(session, Settings(), EventBus())
    winner = service.propose(champion.id, parameters={"fast": 5, "slow": 15})
    loser = service.propose(champion.id, parameters={"fast": 8, "slow": 20})
    assert winner.origin == "challenger" and winner.parent_strategy_id == champion.id
    await engine.run_until_paper(winner)
    await engine.run_until_paper(loser)
    session.refresh(champion)
    # The champion is never modified or replaced by a challenger.
    assert champion.status == "paper_testing" and champion.versions[0].content_hash == champion_hash
    comparison = winner.evidence["robustness"]["challenger_comparison"]
    assert winner.stage == "PAPER_TESTING" and comparison["passed"]
    assert comparison["challenger_oos_expectancy_pct"] > comparison["champion_oos_expectancy_pct"]
    challenger_strategy = session.get(Strategy, winner.strategy_id)
    assert challenger_strategy.parent_strategy_id == champion.id
    assert session.scalar(select(ValidationResult).where(
        ValidationResult.strategy_version_id == challenger_strategy.versions[0].id,
        ValidationResult.method == "challenger_comparison")).result == "PASS"
    assert loser.stage == "FAILED" and not loser.evidence["robustness"]["checks"]["beats_champion"]["passed"]
    with pytest.raises(Exception, match="Unknown champion parameters"):
        service.propose(champion.id, parameters={"nope": 1})
