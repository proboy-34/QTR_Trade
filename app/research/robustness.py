"""Out-of-sample, walk-forward and robustness evaluation with overfitting safeguards."""

import hashlib
from dataclasses import asdict, dataclass
from math import e, sqrt
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from app.global_services.regime import RegimeClassifier
from app.research.dsl import CompiledStrategy, StrategySpec
from app.research.spec_backtest import CostModel, SpecBacktestEngine

EULER_GAMMA = 0.5772156649


@dataclass(frozen=True)
class ValidationGates:
    """Fixed, code-reviewed acceptance thresholds. Neither AI nor API input can loosen them."""

    min_is_trades: int = 15
    min_is_profit_factor: float = 1.0
    max_is_drawdown_pct: float = -30.0
    min_oos_trades: int = 8
    min_oos_profit_factor: float = 1.0
    min_oos_to_is_expectancy: float = 0.25
    min_wf_windows: int = 3
    min_wf_profitable_share: float = 0.5
    min_wf_profit_factor: float = 1.0
    min_perturbation_stability: float = 0.6
    min_stressed_return_pct: float = 0.0
    min_mc_p5_drawdown_pct: float = -35.0
    max_mc_loss_probability: float = 0.4
    min_deflated_sharpe: float = 0.95
    min_oos_excess_return_pct: float = 0.0
    is_fraction: float = 0.7
    min_paper_trades: int = 10
    min_paper_profit_factor: float = 1.0


def split_in_out(frame: pd.DataFrame, is_fraction: float) -> int:
    return int(len(frame) * is_fraction)


def deflated_sharpe(
    sharpe: float, observations: int, trials: int, trial_sharpe_variance: float,
    skew: float = 0.0, kurtosis: float = 3.0,
) -> dict[str, float]:
    """Bailey & Lopez de Prado deflated Sharpe ratio (non-annualized per-observation Sharpe).

    Returns the probability that the true Sharpe exceeds the maximum expected from
    ``trials`` unskilled attempts. More trials raise the bar (data-snooping penalty).
    """
    normal = NormalDist()
    variance = max(trial_sharpe_variance, 1e-8)
    if trials < 2:
        benchmark = 0.0
    else:
        benchmark = sqrt(variance) * (
            (1 - EULER_GAMMA) * normal.inv_cdf(1 - 1 / trials)
            + EULER_GAMMA * normal.inv_cdf(1 - 1 / (trials * e))
        )
    denominator = 1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2
    if observations < 2 or denominator <= 0:
        return {"deflated_sharpe": 0.0, "benchmark_sharpe": benchmark}
    statistic = (sharpe - benchmark) * sqrt(observations - 1) / sqrt(denominator)
    return {"deflated_sharpe": round(normal.cdf(statistic), 6), "benchmark_sharpe": round(benchmark, 8)}


def monte_carlo(trade_returns_pct: list[float], simulations: int = 1000, seed: int = 7) -> dict[str, Any]:
    """Bootstrap the trade sequence to estimate adverse paths (compounded per-trade returns)."""
    if len(trade_returns_pct) < 5:
        return {"simulations": 0, "note": "fewer than 5 trades; distribution not estimated"}
    rng = np.random.default_rng(seed)
    returns = np.array(trade_returns_pct) / 100
    samples = rng.choice(returns, size=(simulations, len(returns)), replace=True)
    curves = np.cumprod(1 + samples * 0.1, axis=1)  # 10% notional per trade approximates sizing
    peaks = np.maximum.accumulate(curves, axis=1)
    drawdowns = ((curves - peaks) / peaks).min(axis=1) * 100
    finals = (curves[:, -1] - 1) * 100
    return {
        "simulations": simulations, "trades": len(returns),
        "return_p5": round(float(np.percentile(finals, 5)), 4),
        "return_p50": round(float(np.percentile(finals, 50)), 4),
        "return_p95": round(float(np.percentile(finals, 95)), 4),
        "max_drawdown_p5": round(float(np.percentile(drawdowns, 5)), 4),
        "max_drawdown_p50": round(float(np.percentile(drawdowns, 50)), 4),
        "loss_probability": round(float((finals < 0).mean()), 4),
        "method": "iid bootstrap of trade returns at 10% notional per trade",
    }


def passive_benchmark(frame: pd.DataFrame, trade_from: int, metrics: dict[str, Any]) -> dict[str, float]:
    """Exposure-adjusted buy-and-hold. A long-only rule must beat simply holding the asset
    for the same fraction of time, otherwise it is riding drift rather than showing an edge."""
    closes = frame["close"].astype(float)
    hold = (closes.iloc[-1] / closes.iloc[trade_from] - 1) * 100 if len(closes) > trade_from else 0.0
    exposure = float(metrics.get("exposure", 0.0))
    return {"buy_and_hold_return_pct": round(float(hold), 4), "exposure": exposure,
            "exposure_adjusted_benchmark_pct": round(float(hold) * exposure, 4),
            "excess_return_pct": round(float(metrics.get("total_return", 0.0)) - float(hold) * exposure, 4)}


def perturbations(spec: StrategySpec) -> list[dict[str, float]]:
    variants: list[dict[str, float]] = []
    for name, value in spec.parameters.items():
        steps = (-2, -1, 1, 2) if abs(value) < 20 else (-0.2, -0.1, 0.1, 0.2)
        for step in steps:
            candidate = value + step if isinstance(step, int) else value * (1 + step)
            candidate = round(candidate) if float(value).is_integer() else round(candidate, 6)
            if candidate > 0 and candidate != value:
                variants.append({**spec.parameters, name: candidate})
    return variants


class RobustnessEvaluator:
    def __init__(self, gates: ValidationGates | None = None, engine: SpecBacktestEngine | None = None) -> None:
        self.gates = gates or ValidationGates()
        self.engine = engine or SpecBacktestEngine()

    def _run(self, spec: StrategySpec, frame: pd.DataFrame, labels: pd.Series, trade_from: int,
             symbol: str, timeframe: str, costs: CostModel) -> dict[str, Any]:
        return self.engine.run(spec, frame, symbol=symbol, timeframe=timeframe, costs=costs,
                               trade_from=trade_from, regimes=labels)

    def in_sample(self, spec: StrategySpec, frame: pd.DataFrame, labels: pd.Series, symbol: str,
                  timeframe: str, costs: CostModel) -> dict[str, Any]:
        cut = split_in_out(frame, self.gates.is_fraction)
        warmup = CompiledStrategy(spec).warmup()
        return self._run(spec, frame.iloc[:cut].reset_index(drop=True), labels.iloc[:cut], warmup, symbol, timeframe, costs)

    def out_of_sample(self, spec: StrategySpec, frame: pd.DataFrame, labels: pd.Series, symbol: str,
                      timeframe: str, costs: CostModel) -> dict[str, Any]:
        cut = split_in_out(frame, self.gates.is_fraction)
        warmup = CompiledStrategy(spec).warmup()
        start = max(0, cut - warmup)  # indicator warm-up only; entries begin at the holdout
        segment = frame.iloc[start:].reset_index(drop=True)
        result = self._run(spec, segment, labels.iloc[start:], cut - start, symbol, timeframe, costs)
        result["benchmark"] = passive_benchmark(segment, cut - start, result["metrics"])
        return result

    def walk_forward(self, spec: StrategySpec, frame: pd.DataFrame, labels: pd.Series, symbol: str,
                     timeframe: str, costs: CostModel, windows: int = 5) -> dict[str, Any]:
        """Rolling holdout windows over the whole history; each window is evaluated unseen.

        Declarative specifications have no fitted parameters, so the training segment is
        indicator warm-up and the question answered is stability across time.
        """
        warmup = CompiledStrategy(spec).warmup()
        usable = len(frame) - warmup
        size = usable // windows
        results: list[dict[str, Any]] = []
        for index in range(windows):
            start = warmup + index * size
            end = start + size if index < windows - 1 else len(frame)
            if end - start < 20:
                continue
            segment = frame.iloc[start - warmup:end].reset_index(drop=True)
            run = self._run(spec, segment, labels.iloc[start - warmup:end], warmup, symbol, timeframe, costs)
            results.append({"start": str(frame["timestamp"].iloc[start]), "end": str(frame["timestamp"].iloc[end - 1]),
                            "metrics": run["metrics"], "trade_returns": [t["return_pct"] for t in run["trades"]],
                            "pnls": [t["pnl"] for t in run["trades"]]})
        pnls = [value for item in results for value in item["pnls"]]
        wins, losses = sum(v for v in pnls if v > 0), -sum(v for v in pnls if v < 0)
        traded = [item for item in results if item["metrics"]["number_of_trades"] > 0]
        return {
            "windows": [{key: value for key, value in item.items() if key != "pnls"} for item in results],
            "aggregate": {
                "windows": len(results),
                "windows_with_trades": len(traded),
                "profitable_share": round(sum(item["metrics"]["net_profit"] > 0 for item in traded) / len(traded), 4) if traded else 0.0,
                "profit_factor": round(wins / losses, 4) if losses else (999.0 if wins else 0.0),
                "trades": len(pnls),
                "average_window_return": round(float(np.mean([item["metrics"]["total_return"] for item in results])), 4) if results else 0.0,
            },
        }

    def robustness(self, spec: StrategySpec, frame: pd.DataFrame, labels: pd.Series, symbol: str,
                   timeframe: str, costs: CostModel, baseline_trades: list[dict[str, Any]],
                   trials: int, trial_sharpe_variance: float, baseline_metrics: dict[str, Any]) -> dict[str, Any]:
        warmup = CompiledStrategy(spec).warmup()
        perturbed: list[dict[str, Any]] = []
        for parameters in perturbations(spec):
            try:
                variant = spec.model_copy(update={"parameters": parameters})
                variant = StrategySpec.model_validate(variant.model_dump())
                metrics = self._run(variant, frame, labels, CompiledStrategy(variant).warmup(), symbol, timeframe, costs)["metrics"]
            except ValueError:
                continue
            perturbed.append({"parameters": parameters, "total_return": metrics["total_return"],
                              "profit_factor": metrics["profit_factor"], "trades": metrics["number_of_trades"]})
        stable = [item for item in perturbed if item["total_return"] > 0 and item["profit_factor"] >= 1]
        stability = round(len(stable) / len(perturbed), 4) if perturbed else 1.0
        stress_cases = {
            "double_fees": CostModel(costs.fee_rate * 2, costs.slippage_rate, costs.spread_bps),
            "triple_slippage": CostModel(costs.fee_rate, costs.slippage_rate * 3, costs.spread_bps),
            "wide_spread": CostModel(costs.fee_rate, costs.slippage_rate, costs.spread_bps + 10),
            "combined_2x": CostModel(costs.fee_rate * 2, costs.slippage_rate * 2, costs.spread_bps * 2),
        }
        stress = {
            name: self._run(spec, frame, labels, warmup, symbol, timeframe, model)["metrics"]["total_return"]
            for name, model in stress_cases.items()
        }
        seed = int(hashlib.sha256(spec.model_dump_json().encode()).hexdigest()[:8], 16)
        simulation = monte_carlo([trade["return_pct"] for trade in baseline_trades], seed=seed)
        dsr = deflated_sharpe(
            float(baseline_metrics.get("per_bar_sharpe", 0)), int(baseline_metrics.get("observations", 0)),
            trials, trial_sharpe_variance, float(baseline_metrics.get("return_skew", 0)),
            float(baseline_metrics.get("return_kurtosis", 3)),
        )
        return {
            "parameter_perturbation": {"variants": perturbed, "stability": stability,
                                       "parameter_count": spec.parameter_count},
            "cost_sensitivity": stress,
            "monte_carlo": simulation,
            "data_snooping": {"trials": trials, "trial_sharpe_variance": trial_sharpe_variance, **dsr},
        }


def regime_labels(frame: pd.DataFrame) -> pd.Series:
    return RegimeClassifier().classify_series(frame)


def gates_dict(gates: ValidationGates) -> dict[str, Any]:
    return asdict(gates)
