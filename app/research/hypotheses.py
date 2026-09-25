"""Hypothesis engine: the controlled path from an idea to a paper-tested candidate.

IDEA -> RESEARCH -> BACKTESTING -> OOS_VALIDATION -> WALK_FORWARD -> ROBUSTNESS
-> PAPER_TESTING -> PAPER_VALIDATED -> CANDIDATE (-> ACTIVE only by a human operator)

Every stage runs reproducible experiments against a frozen dataset range, records its
checks, and failed or rejected ideas stay in research memory. Research writes only to
the Strategy Repository; it never creates decisions, intents or orders.
"""

from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.core.lineage import code_version, fingerprint
from app.core.time import TimeService
from app.global_services.regime import candles_frame
from app.global_services.strategy_repository import StrategyRepository
from app.global_services.universe import UniverseService
from app.memory.knowledge import KnowledgeBase
from app.models import (
    Dataset,
    Experiment,
    Hypothesis,
    HypothesisStage,
    MarketCandle,
    Strategy,
    ValidationResult,
)
from app.research.dsl import StrategySpec, spec_from_version, validation_errors, version_payload
from app.research.experiments import ExperimentManager
from app.research.robustness import RobustnessEvaluator, ValidationGates, gates_dict, regime_labels
from app.research.spec_backtest import CostModel

TERMINAL = {HypothesisStage.REJECTED, HypothesisStage.FAILED, HypothesisStage.ARCHIVED,
            HypothesisStage.ACTIVE, HypothesisStage.CANDIDATE}
QUANT_STAGES = ("BACKTESTING", "OOS_VALIDATION", "WALK_FORWARD", "ROBUSTNESS")


def _ignore_config(function: Callable[[], dict[str, Any]]) -> Callable[[dict], dict[str, Any]]:
    """Experiment runners receive their stored configuration; these closures already hold it."""
    return lambda _configuration: function()


def _check(value: float, threshold: float, minimum: bool = True) -> dict[str, Any]:
    value = float(value)
    return {"value": round(value, 6), "threshold": threshold, "passed": value >= threshold if minimum else value <= threshold}


def pooled(runs: list[dict[str, Any]]) -> dict[str, Any]:
    trades = [trade for run in runs for trade in run["trades"]]
    pnls = np.array([trade["pnl"] for trade in trades], dtype=float)
    wins, losses = pnls[pnls > 0].sum(), -pnls[pnls < 0].sum()
    returns = [trade["return_pct"] for trade in trades]
    return {
        "trades": len(trades),
        "profit_factor": round(float(wins / losses), 4) if losses else (999.0 if wins else 0.0),
        "expectancy_pct": round(float(np.mean(returns)), 5) if returns else 0.0,
        "win_rate": round(float((pnls > 0).mean() * 100), 2) if len(pnls) else 0.0,
        "net_profit": round(float(pnls.sum()), 2),
        "worst_drawdown": min((run["metrics"]["maximum_drawdown"] for run in runs), default=0.0),
        "sharpe_ratio": round(float(np.mean([run["metrics"]["sharpe_ratio"] for run in runs])), 4) if runs else 0.0,
        "sortino_ratio": round(float(np.mean([run["metrics"]["sortino_ratio"] for run in runs])), 4) if runs else 0.0,
        "fees_paid": round(float(sum(run["metrics"]["fees_paid"] for run in runs)), 2),
        "turnover": round(float(sum(run["metrics"]["turnover"] for run in runs)), 4),
    }


class HypothesisEngine:
    def __init__(
        self, session: Session, settings: Settings, event_bus: EventBus,
        gates: ValidationGates | None = None, min_bars: int = 300, max_assets: int = 3,
    ) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus
        self.gates = gates or ValidationGates()
        self.min_bars = min_bars
        self.max_assets = max_assets
        self.evaluator = RobustnessEvaluator(self.gates)
        self.knowledge = KnowledgeBase(session)
        self.costs = CostModel(settings.paper_fee_rate, settings.paper_slippage_rate, settings.paper_spread_bps)

    # ----------------------------------------------------------------- creation
    def create(
        self, statement: str, *, origin: str = "operator", spec: dict[str, Any] | None = None,
        description: str = "", market_conditions: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None, assumptions: list[str] | None = None,
        parent_strategy_id: str | None = None, ai_artifact_id: str | None = None,
        observation_id: str | None = None,
    ) -> Hypothesis:
        spec = spec or {}
        errors = validation_errors(spec) if spec else ["spec: a declarative strategy specification is required"]
        hypothesis = Hypothesis(
            statement=statement, origin=origin, description=description, spec=spec,
            market_conditions=market_conditions or {}, variables=variables or {},
            assumptions=assumptions or [], parent_strategy_id=parent_strategy_id,
            ai_artifact_id=ai_artifact_id, observation_id=observation_id,
            assets=[str(item).upper() for item in spec.get("universe", [])] if isinstance(spec.get("universe"), list) else [],
            timeframes=list(spec.get("timeframes", [])) if isinstance(spec.get("timeframes"), list) else [],
            test_definition={"engine": "spec_backtest", "gates": gates_dict(self.gates)},
            stage=HypothesisStage.IDEA, stage_history=[], trials=0,
        )
        self.session.add(hypothesis)
        self.session.flush()
        self._stage(hypothesis, HypothesisStage.IDEA, "created")
        if errors:
            self._reject(hypothesis, HypothesisStage.REJECTED, "INVALID_SPEC", {"errors": errors})
        self.session.commit()
        return hypothesis

    # ------------------------------------------------------------------ helpers
    def _stage(self, hypothesis: Hypothesis, stage: str, reason: str, detail: dict[str, Any] | None = None) -> None:
        hypothesis.stage = stage
        hypothesis.stage_history = [*(hypothesis.stage_history or []), {
            "stage": stage, "at": TimeService.now().isoformat(), "reason": reason, **(detail or {}),
        }]

    def _reject(self, hypothesis: Hypothesis, stage: str, code: str, evidence: dict[str, Any]) -> None:
        self._stage(hypothesis, stage, code)
        hypothesis.decision_reason = code
        hypothesis.result = "rejected" if stage == HypothesisStage.REJECTED else "refuted"
        self.knowledge.record(
            "FAILED_IDEA", f"{code}: {hypothesis.statement[:200]}",
            body=f"Hypothesis {hypothesis.id} stopped at {stage} ({code}).",
            evidence=evidence, refs={"hypothesis_id": hypothesis.id},
            tags=[hypothesis.origin, code], symbol=next(iter(hypothesis.assets or []), None),
            timeframe=next(iter(hypothesis.timeframes or []), None), dedup_key=f"failed:{hypothesis.id}",
        )

    def _spec(self, hypothesis: Hypothesis) -> StrategySpec:
        return StrategySpec.model_validate(hypothesis.spec)

    def _frame(self, scope: dict[str, Any]) -> pd.DataFrame:
        start = datetime.fromisoformat(scope["start"])
        end = datetime.fromisoformat(scope["end"])
        rows = self.session.scalars(select(MarketCandle).where(
            MarketCandle.exchange == scope["exchange"], MarketCandle.symbol == scope["symbol"],
            MarketCandle.timeframe == scope["timeframe"], MarketCandle.timestamp >= start,
            MarketCandle.timestamp <= end,
        ).order_by(MarketCandle.timestamp)).all()
        return candles_frame(list(rows))

    def _trial_statistics(self, timeframe: str) -> tuple[int, float]:
        rows = self.session.scalars(select(Experiment).where(Experiment.status == "COMPLETED")).all()
        sharpes = [
            float((row.result or {}).get("metrics", {}).get("per_bar_sharpe", 0))
            for row in rows
            if (row.configuration or {}).get("stage") in {"BACKTESTING", "PERTURBATION"}
            and (row.configuration or {}).get("timeframe") == timeframe
        ]
        variance = float(np.var(sharpes)) if len(sharpes) > 1 else 1e-4
        return max(1, len(sharpes)), max(variance, 1e-6)

    def _experiment(self, hypothesis: Hypothesis, stage: str, scope: dict[str, Any], runner) -> dict[str, Any]:
        configuration = {
            "hypothesis_id": hypothesis.id, "stage": stage, "spec": hypothesis.spec,
            "exchange": scope["exchange"], "symbol": scope["symbol"], "timeframe": scope["timeframe"],
            "dataset_fingerprint": scope["fingerprint"], "costs": self.costs.__dict__,
            "gates": gates_dict(self.gates), "engine": "spec_backtest/v1",
        }
        manager = ExperimentManager(self.session)
        experiment = manager.create(f"{stage} · {scope['symbol']} {scope['timeframe']} · H-{hypothesis.id[:8]}", configuration)
        experiment.dataset_id = scope["dataset_id"]
        if experiment.status == "FAILED":
            manager.transition(experiment, "QUEUED")
        if experiment.status == "QUEUED":
            manager.execute(experiment, runner)
            experiment.reproducibility = {**(experiment.reproducibility or {}), "code_version": code_version()}
            self.session.commit()
        hypothesis.trials = (hypothesis.trials or 0) + 1
        evidence = dict(hypothesis.evidence or {})
        evidence["experiment_ids"] = sorted({*evidence.get("experiment_ids", []), experiment.id})
        hypothesis.evidence = evidence
        if experiment.status != "COMPLETED":
            raise RuntimeError(experiment.error or "experiment did not complete")
        return experiment.result

    def _dataset(self, exchange: str, symbol: str, timeframe: str, frame: pd.DataFrame) -> dict[str, Any]:
        start, end = pd.Timestamp(frame["timestamp"].iloc[0]), pd.Timestamp(frame["timestamp"].iloc[-1])
        digest = fingerprint({"exchange": exchange, "symbol": symbol, "timeframe": timeframe,
                              "start": str(start), "end": str(end), "rows": len(frame),
                              "close_sum": round(float(frame["close"].sum()), 8)})
        name = f"{exchange}:{symbol}:{timeframe}:research:{digest[:12]}"
        dataset = self.session.scalar(select(Dataset).where(Dataset.name == name))
        if not dataset:
            dataset = Dataset(name=name, symbol=symbol, timeframe=timeframe, source=f"{exchange}-research-snapshot",
                              row_count=len(frame), freshness_at=end.to_pydatetime(), is_demo=exchange == "paper")
            self.session.add(dataset)
            self.session.flush()
        return {"exchange": exchange, "symbol": symbol, "timeframe": timeframe, "dataset_id": dataset.id,
                "fingerprint": digest, "bars": len(frame),
                "start": start.to_pydatetime().replace(tzinfo=None).isoformat(),
                "end": end.to_pydatetime().replace(tzinfo=None).isoformat()}

    # ---------------------------------------------------------------- lifecycle
    async def advance(self, hypothesis: Hypothesis) -> dict[str, Any]:
        stage = hypothesis.stage
        if stage in TERMINAL:
            return {"hypothesis_id": hypothesis.id, "stage": stage, "changed": False}
        if not hypothesis.spec:
            # Legacy/manual hypotheses without a specification are kept but cannot be tested.
            return {"hypothesis_id": hypothesis.id, "stage": stage, "changed": False,
                    "reason": "NO_SPECIFICATION: attach a declarative strategy specification to research it"}
        handler = {
            HypothesisStage.IDEA: self._research,
            HypothesisStage.RESEARCH: self._backtest,
            HypothesisStage.BACKTESTING: self._oos,
            HypothesisStage.OOS_VALIDATION: self._walk_forward,
            HypothesisStage.WALK_FORWARD: self._robustness,
            HypothesisStage.ROBUSTNESS: self._promote_to_paper,
            HypothesisStage.PAPER_TESTING: self._paper_validation,
            HypothesisStage.PAPER_VALIDATED: self._candidate,
            HypothesisStage.DEGRADED: self._noop,
        }.get(HypothesisStage(stage), self._noop)
        try:
            await handler(hypothesis)
        except Exception as exc:  # a broken experiment is research evidence, not a crash
            self.session.rollback()
            hypothesis = self.session.get(Hypothesis, hypothesis.id) or hypothesis
            self._reject(hypothesis, HypothesisStage.FAILED, "EXPERIMENT_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
        self.session.commit()
        return {"hypothesis_id": hypothesis.id, "from": stage, "stage": hypothesis.stage,
                "changed": stage != hypothesis.stage, "reason": hypothesis.decision_reason}

    async def run_until_paper(self, hypothesis: Hypothesis, max_steps: int = 8) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _ in range(max_steps):
            result = await self.advance(hypothesis)
            if not result["changed"] or hypothesis.stage in {HypothesisStage.PAPER_TESTING, *TERMINAL}:
                break
        return result

    async def _noop(self, hypothesis: Hypothesis) -> None:
        return None

    async def _research(self, hypothesis: Hypothesis) -> None:
        spec = self._spec(hypothesis)
        timeframe = spec.timeframes[0]
        variables = hypothesis.variables or {}
        preferred = [variables.get("exchange"), self.settings.scanner_exchange, "binance", "paper"]
        exchanges = list(dict.fromkeys(item for item in preferred if item))
        symbols: list[str] = []
        for entry in spec.universe:
            if entry == "ELIGIBLE":
                for exchange in exchanges:
                    symbols.extend(UniverseService.eligible_symbols(self.session, exchange))
            else:
                symbols.append(entry)
        symbols = list(dict.fromkeys(symbols))
        scopes: list[dict[str, Any]] = []
        shortfalls: dict[str, int] = {}
        for symbol in symbols:
            for exchange in exchanges:
                rows = self.session.scalars(select(MarketCandle).where(
                    MarketCandle.exchange == exchange, MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe,
                ).order_by(MarketCandle.timestamp)).all()
                if len(rows) >= self.min_bars:
                    scopes.append(self._dataset(exchange, symbol, timeframe, candles_frame(list(rows))))
                    break
                shortfalls[f"{exchange}:{symbol}"] = len(rows)
            if len(scopes) >= self.max_assets:
                break
        hypothesis.assets = [scope["symbol"] for scope in scopes] or symbols
        hypothesis.timeframes = [timeframe]
        if not scopes:
            self._reject(hypothesis, HypothesisStage.REJECTED, "INSUFFICIENT_DATA",
                         {"required_bars": self.min_bars, "available": shortfalls})
            return
        hypothesis.evidence = {**(hypothesis.evidence or {}), "scope": scopes,
                               "parameter_count": spec.parameter_count, "code_version": code_version()}
        self._stage(hypothesis, HypothesisStage.RESEARCH, "data available", {"assets": hypothesis.assets})

    def _runs(self, hypothesis: Hypothesis, stage: str, method: str) -> list[dict[str, Any]]:
        spec = self._spec(hypothesis)
        runs = []
        for scope in hypothesis.evidence["scope"]:
            frame = self._frame(scope)
            labels = regime_labels(frame)
            evaluate = partial(getattr(self.evaluator, method), spec, frame, labels, scope["symbol"],
                               scope["timeframe"], self.costs)
            runs.append(self._experiment(hypothesis, stage, scope, _ignore_config(evaluate)))
        return runs

    def _gate(self, hypothesis: Hypothesis, key: str, summary: dict[str, Any], checks: dict[str, dict[str, Any]],
              next_stage: str, extra: dict[str, Any] | None = None) -> bool:
        passed = all(item["passed"] for item in checks.values())
        evidence = dict(hypothesis.evidence or {})
        evidence[key] = {"result": "PASS" if passed else "FAIL", "summary": summary, "checks": checks, **(extra or {})}
        hypothesis.evidence = evidence
        if passed:
            self._stage(hypothesis, next_stage, f"{key} passed")
        else:
            failed = [name for name, item in checks.items() if not item["passed"]]
            self._reject(hypothesis, HypothesisStage.FAILED, f"{key.upper()}_FAILED", {"failed_checks": failed, "summary": summary})
        return passed

    async def _backtest(self, hypothesis: Hypothesis) -> None:
        runs = self._runs(hypothesis, "BACKTESTING", "in_sample")
        summary = pooled(runs)
        summary["regimes"] = {run_scope["symbol"]: run["regimes"] for run_scope, run in zip(hypothesis.evidence["scope"], runs, strict=True)}
        self._gate(hypothesis, "backtest", summary, {
            "trades": _check(summary["trades"], self.gates.min_is_trades),
            "profit_factor": _check(summary["profit_factor"], self.gates.min_is_profit_factor),
            "max_drawdown": _check(summary["worst_drawdown"], self.gates.max_is_drawdown_pct),
        }, HypothesisStage.BACKTESTING)

    async def _oos(self, hypothesis: Hypothesis) -> None:
        runs = self._runs(hypothesis, "OOS_VALIDATION", "out_of_sample")
        summary = pooled(runs)
        in_sample = hypothesis.evidence["backtest"]["summary"]
        ratio = summary["expectancy_pct"] / in_sample["expectancy_pct"] if in_sample["expectancy_pct"] > 0 else 0.0
        summary["oos_to_is_expectancy"] = round(ratio, 4)
        excess = float(np.mean([run["benchmark"]["excess_return_pct"] for run in runs]))
        summary["excess_over_passive_pct"] = round(excess, 4)
        summary["benchmarks"] = [run["benchmark"] for run in runs]
        self._gate(hypothesis, "out_of_sample", summary, {
            "trades": _check(summary["trades"], self.gates.min_oos_trades),
            "profit_factor": _check(summary["profit_factor"], self.gates.min_oos_profit_factor),
            "expectancy_positive": _check(summary["expectancy_pct"], 1e-9),
            "is_to_oos_decay": _check(ratio, self.gates.min_oos_to_is_expectancy),
            "beats_passive_exposure": _check(excess, self.gates.min_oos_excess_return_pct + 1e-9),
        }, HypothesisStage.OOS_VALIDATION)

    async def _walk_forward(self, hypothesis: Hypothesis) -> None:
        runs = self._runs(hypothesis, "WALK_FORWARD", "walk_forward")
        windows = sum(run["aggregate"]["windows"] for run in runs)
        traded = sum(run["aggregate"]["windows_with_trades"] for run in runs)
        profitable = float(np.mean([run["aggregate"]["profitable_share"] for run in runs]))
        factors = [run["aggregate"]["profit_factor"] for run in runs]
        summary = {"windows": windows, "windows_with_trades": traded, "profitable_share": round(profitable, 4),
                   "profit_factor": round(float(np.mean(factors)), 4),
                   "per_asset": [run["aggregate"] for run in runs]}
        self._gate(hypothesis, "walk_forward", summary, {
            "windows": _check(traded, self.gates.min_wf_windows),
            "profitable_share": _check(profitable, self.gates.min_wf_profitable_share),
            "profit_factor": _check(summary["profit_factor"], self.gates.min_wf_profit_factor),
        }, HypothesisStage.WALK_FORWARD)

    async def _robustness(self, hypothesis: Hypothesis) -> None:
        spec = self._spec(hypothesis)
        trials, variance = self._trial_statistics(spec.timeframes[0])
        results = []
        for scope in hypothesis.evidence["scope"]:
            frame = self._frame(scope)
            labels = regime_labels(frame)
            baseline = self.evaluator.engine.run(spec, frame, symbol=scope["symbol"], timeframe=scope["timeframe"],
                                                 costs=self.costs, trade_from=0, regimes=labels)
            n_trials = trials + len(spec.parameters) * 4
            evaluate = partial(self.evaluator.robustness, spec, frame, labels, scope["symbol"], scope["timeframe"],
                               self.costs, baseline["trades"], n_trials, variance, baseline["metrics"])
            result = self._experiment(hypothesis, "ROBUSTNESS", scope, _ignore_config(evaluate))
            result["regimes"] = baseline["regimes"]
            results.append(result)
        stability = float(np.mean([item["parameter_perturbation"]["stability"] for item in results]))
        stressed = float(min(item["cost_sensitivity"]["combined_2x"] for item in results))
        simulations = [item["monte_carlo"] for item in results if item["monte_carlo"].get("simulations")]
        mc_drawdown = min((item["max_drawdown_p5"] for item in simulations), default=-100.0)
        mc_loss = max((item["loss_probability"] for item in simulations), default=1.0)
        dsr = float(np.mean([item["data_snooping"]["deflated_sharpe"] for item in results]))
        summary = {
            "perturbation_stability": round(stability, 4), "stressed_return_pct": round(stressed, 4),
            "monte_carlo_p5_drawdown": mc_drawdown, "monte_carlo_loss_probability": mc_loss,
            "deflated_sharpe": round(dsr, 4), "trials": results[0]["data_snooping"]["trials"] if results else 0,
            "parameter_count": spec.parameter_count, "per_asset": results,
        }
        checks = {
            "perturbation_stability": _check(stability, self.gates.min_perturbation_stability),
            "cost_stress": _check(stressed, self.gates.min_stressed_return_pct + 1e-9),
            "monte_carlo_drawdown": _check(mc_drawdown, self.gates.min_mc_p5_drawdown_pct),
            "monte_carlo_loss_probability": _check(mc_loss, self.gates.max_mc_loss_probability, minimum=False),
            "deflated_sharpe": _check(dsr, self.gates.min_deflated_sharpe),
        }
        extra: dict[str, Any] = {}
        if hypothesis.parent_strategy_id:
            comparison = self._challenger_comparison(hypothesis)
            extra["challenger_comparison"] = comparison
            checks["beats_champion"] = {"value": comparison["challenger_oos_expectancy_pct"],
                                        "threshold": comparison["champion_oos_expectancy_pct"],
                                        "passed": comparison["passed"]}
        self._gate(hypothesis, "robustness", summary, checks, HypothesisStage.ROBUSTNESS, extra)

    def _challenger_comparison(self, hypothesis: Hypothesis) -> dict[str, Any]:
        champion = self.session.get(Strategy, hypothesis.parent_strategy_id)
        champion_spec = None
        if champion and champion.versions:
            champion_spec = spec_from_version(max(champion.versions, key=lambda item: item.version), champion)
        if champion_spec is None:
            return {"passed": False, "reason": "champion has no declarative specification",
                    "challenger_oos_expectancy_pct": 0.0, "champion_oos_expectancy_pct": 0.0}
        champion_runs = []
        for scope in hypothesis.evidence["scope"]:
            frame = self._frame(scope)
            champion_runs.append(self.evaluator.out_of_sample(champion_spec, frame, regime_labels(frame),
                                                              scope["symbol"], scope["timeframe"], self.costs))
        champion_summary = pooled(champion_runs)
        challenger_summary = hypothesis.evidence["out_of_sample"]["summary"]
        passed = (challenger_summary["expectancy_pct"] > champion_summary["expectancy_pct"]
                  and challenger_summary["worst_drawdown"] >= champion_summary["worst_drawdown"] - 5)
        return {"passed": passed, "champion_strategy_id": hypothesis.parent_strategy_id,
                "challenger_oos_expectancy_pct": challenger_summary["expectancy_pct"],
                "champion_oos_expectancy_pct": champion_summary["expectancy_pct"],
                "challenger_worst_drawdown": challenger_summary["worst_drawdown"],
                "champion_worst_drawdown": champion_summary["worst_drawdown"],
                "rule": "challenger OOS expectancy must exceed champion; drawdown may not be >5pts worse"}

    async def _promote_to_paper(self, hypothesis: Hypothesis) -> None:
        spec = self._spec(hypothesis)
        scopes = hypothesis.evidence["scope"]
        symbols = [scope["symbol"] for scope in scopes]
        universe_spec = spec.model_copy(update={"universe": symbols if "ELIGIBLE" not in spec.universe else spec.universe})
        documentation = (
            f"Research candidate from hypothesis {hypothesis.id} ({hypothesis.origin}). {hypothesis.statement}\n"
            f"Validated on {', '.join(scope['exchange'] + ':' + scope['symbol'] for scope in scopes)} {spec.timeframes[0]} "
            f"with next-bar execution, costs {self.costs.__dict__}, {hypothesis.trials} experiments."
        )
        repository = StrategyRepository(self.session)
        strategy = repository.create({
            "name": f"{hypothesis.statement[:120]} [H-{hypothesis.id[:8]}]",
            "description": hypothesis.description or hypothesis.statement,
            "symbol": symbols[0] if len(symbols) == 1 else "MULTI",
            "timeframe": spec.timeframes[0], "author": "QTR research", "origin": hypothesis.origin,
            "parent_strategy_id": hypothesis.parent_strategy_id, "hypothesis_id": hypothesis.id,
            "exchange": scopes[0]["exchange"],
            "version": version_payload(universe_spec, documentation),
        })
        version = strategy.versions[0]
        for method, key in (("backtest", "backtest"), ("out_of_sample", "out_of_sample"),
                            ("walk_forward", "walk_forward"), ("robustness", "robustness")):
            evidence = hypothesis.evidence[key]
            self.session.add(ValidationResult(
                strategy_version_id=version.id, dataset_id=scopes[0]["dataset_id"], method=method,
                result=evidence["result"], metrics=evidence["summary"],
                rules={name: check["threshold"] for name, check in evidence["checks"].items()},
                configuration={"hypothesis_id": hypothesis.id, "costs": self.costs.__dict__,
                               "datasets": [scope["fingerprint"] for scope in scopes],
                               "experiment_ids": hypothesis.evidence.get("experiment_ids", []),
                               "trials": hypothesis.trials, "code_version": code_version()},
                notes=f"Automated research gate for hypothesis {hypothesis.id}",
            ))
        if "challenger_comparison" in hypothesis.evidence["robustness"]:
            comparison = hypothesis.evidence["robustness"]["challenger_comparison"]
            self.session.add(ValidationResult(
                strategy_version_id=version.id, method="challenger_comparison",
                result="PASS" if comparison["passed"] else "FAIL", metrics=comparison,
                rules={"rule": comparison.get("rule")}, configuration={"hypothesis_id": hypothesis.id},
                notes="Challenger evaluated against the unchanged champion",
            ))
        self.session.flush()
        repository.transition(strategy.id, "under_validation", "automated research gates", actor="research")
        repository.transition(strategy.id, "paper_testing", "all research gates passed", actor="research")
        hypothesis.strategy_id = strategy.id
        self._stage(hypothesis, HypothesisStage.PAPER_TESTING, "candidate created for paper validation",
                    {"strategy_id": strategy.id})
        self.knowledge.record(
            "FINDING", f"Candidate passed research gates: {hypothesis.statement[:200]}",
            body=f"Strategy {strategy.id} entered paper testing after {hypothesis.trials} experiments.",
            evidence={key: hypothesis.evidence[key]["summary"] for key in ("backtest", "out_of_sample")},
            refs={"hypothesis_id": hypothesis.id, "strategy_id": strategy.id},
            tags=[hypothesis.origin, "candidate"], symbol=symbols[0], timeframe=spec.timeframes[0],
            dedup_key=f"candidate:{hypothesis.id}",
        )
        await publish_persisted(self.session, self.event_bus, Event("RESEARCH_CANDIDATE_CREATED", {
            "hypothesis_id": hypothesis.id, "strategy_id": strategy.id, "strategy_version_id": version.id,
        }, source="hypothesis_engine"), "research")

    async def _paper_validation(self, hypothesis: Hypothesis) -> None:
        from app.learning.paper_validation import PaperValidationService

        strategy = self.session.get(Strategy, hypothesis.strategy_id) if hypothesis.strategy_id else None
        if not strategy:
            self._reject(hypothesis, HypothesisStage.FAILED, "CANDIDATE_MISSING", {})
            return
        verdict = PaperValidationService(self.session, self.gates).evaluate(strategy)
        evidence = dict(hypothesis.evidence or {})
        evidence["paper_validation"] = verdict
        hypothesis.evidence = evidence
        if verdict["result"] == "INSUFFICIENT_DATA":
            hypothesis.decision_reason = "WAITING_FOR_PAPER_TRADES"
            return
        if verdict["result"] == "PASS":
            self._stage(hypothesis, HypothesisStage.PAPER_VALIDATED, "paper validation passed")
            hypothesis.decision_reason = ""
            return
        StrategyRepository(self.session).transition(strategy.id, "under_review", "paper validation failed", actor="research")
        self._reject(hypothesis, HypothesisStage.FAILED, "PAPER_VALIDATION_FAILED", verdict)

    async def _candidate(self, hypothesis: Hypothesis) -> None:
        strategy = self.session.get(Strategy, hypothesis.strategy_id)
        if strategy is None:
            return
        StrategyRepository(self.session).transition(strategy.id, "ready_for_review",
                                                   "paper validation passed; awaiting human review", actor="research")
        self._stage(hypothesis, HypothesisStage.CANDIDATE, "ready for human review", {"strategy_id": strategy.id})
        hypothesis.result = "supported"


def research_counts(session: Session) -> dict[str, Any]:
    stages = dict(session.execute(select(Hypothesis.stage, func.count()).group_by(Hypothesis.stage)).all())
    experiments = dict(session.execute(select(Experiment.status, func.count()).group_by(Experiment.status)).all())
    running_stages = {"RESEARCH", "BACKTESTING", "OOS_VALIDATION", "WALK_FORWARD", "ROBUSTNESS"}
    return {
        "hypotheses": sum(stages.values()),
        "experiments": sum(experiments.values()),
        "running_experiments": experiments.get("RUNNING", 0),
        "in_research": sum(value for key, value in stages.items() if key in running_stages),
        "rejected": stages.get("REJECTED", 0) + stages.get("FAILED", 0),
        "promising": stages.get("ROBUSTNESS", 0),
        "paper_testing": stages.get("PAPER_TESTING", 0),
        "validated": stages.get("PAPER_VALIDATED", 0) + stages.get("CANDIDATE", 0),
        "active": stages.get("ACTIVE", 0),
        "by_stage": stages,
        "experiments_by_status": experiments,
    }
