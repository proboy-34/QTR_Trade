"""Paper validation of research candidates and backtest-vs-paper discrepancy analysis."""

from typing import Any

import numpy as np
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import DiscrepancyReport, Strategy, TradeMemory, ValidationResult
from app.research.robustness import ValidationGates


def trade_statistics(trades: list[TradeMemory]) -> dict[str, Any]:
    returns = np.array([trade.return_pct for trade in trades], dtype=float)
    pnls = np.array([float(trade.net_pnl) for trade in trades], dtype=float)
    wins, losses = pnls[pnls > 0].sum(), -pnls[pnls < 0].sum()
    notional = [float(trade.entry_price) * float(trade.quantity) for trade in trades]
    slippage_bps = [float(trade.slippage_cost) / value * 10_000 for trade, value in zip(trades, notional, strict=True) if value > 0]
    return {
        "trades": len(trades),
        "expectancy_pct": round(float(returns.mean()), 5) if len(returns) else 0.0,
        "stderr_pct": round(float(returns.std(ddof=1) / np.sqrt(len(returns))), 5) if len(returns) > 1 else None,
        "profit_factor": round(float(wins / losses), 4) if losses else (999.0 if wins else 0.0),
        "win_rate": round(float((pnls > 0).mean() * 100), 2) if len(pnls) else 0.0,
        "net_pnl": round(float(pnls.sum()), 4),
        "average_slippage_bps": round(float(np.mean(slippage_bps)), 3) if slippage_bps else 0.0,
        "average_holding_hours": round(float(np.mean([t.holding_seconds for t in trades]) / 3600), 3) if trades else 0.0,
        "regime_mix": {regime: sum(1 for t in trades if t.regime_at_entry == regime) for regime in {t.regime_at_entry for t in trades}},
        "exit_reasons": {reason: sum(1 for t in trades if t.exit_reason == reason) for reason in {t.exit_reason for t in trades}},
    }


class PaperValidationService:
    def __init__(self, session: Session, gates: ValidationGates | None = None) -> None:
        self.session = session
        self.gates = gates or ValidationGates()

    def _latest_version(self, strategy: Strategy):
        return max(strategy.versions, key=lambda item: item.version)

    def evaluate(self, strategy: Strategy) -> dict[str, Any]:
        version = self._latest_version(strategy)
        trades = list(self.session.scalars(select(TradeMemory).where(
            TradeMemory.strategy_version_id == version.id
        ).order_by(TradeMemory.closed_at)).all())
        paper = trade_statistics(trades)
        if len(trades) < self.gates.min_paper_trades:
            return {"result": "INSUFFICIENT_DATA", "paper": paper, "required_trades": self.gates.min_paper_trades}
        report = self.discrepancy(strategy, trades, paper)
        expected = report.backtest.get("expectancy_pct", 0.0)
        tolerance = 2 * (paper["stderr_pct"] or 0.0)
        checks = {
            "positive_expectancy": paper["expectancy_pct"] > 0,
            "profit_factor": paper["profit_factor"] >= self.gates.min_paper_profit_factor,
            "consistent_with_backtest": paper["expectancy_pct"] >= expected - tolerance,
        }
        result = "PASS" if all(checks.values()) else "FAIL"
        self.session.add(ValidationResult(
            strategy_version_id=version.id, method="paper_validation", result=result,
            metrics={"paper": paper, "backtest_expectation": report.backtest, "discrepancy_report_id": report.id},
            rules={"min_trades": self.gates.min_paper_trades, "min_profit_factor": self.gates.min_paper_profit_factor,
                   "tolerance": "backtest OOS expectancy minus 2 standard errors"},
            configuration={"trade_ids": [trade.id for trade in trades]}, notes="Paper-trading validation",
        ))
        self.session.flush()
        return {"result": result, "checks": checks, "paper": paper, "discrepancy_report_id": report.id}

    def discrepancy(self, strategy: Strategy, trades: list[TradeMemory], paper: dict[str, Any] | None = None) -> DiscrepancyReport:
        version = self._latest_version(strategy)
        paper = paper or trade_statistics(trades)
        oos = self.session.scalar(select(ValidationResult).where(
            ValidationResult.strategy_version_id == version.id,
            ValidationResult.method.in_(["out_of_sample", "backtest", "walk_forward"]),
        ).order_by(desc(ValidationResult.created_at)))
        metrics = (oos.metrics or {}) if oos else {}
        # Legacy walk-forward records report window returns, not per-trade expectancy (treated as 0).
        expectancy = metrics.get("expectancy_pct")
        backtest = {"method": oos.method if oos else None, "expectancy_pct": float(expectancy or 0.0),
                    "profit_factor": metrics.get("profit_factor"), "win_rate": metrics.get("win_rate")}
        differences = {
            "expectancy_pct": round(paper["expectancy_pct"] - backtest["expectancy_pct"], 5),
            "win_rate": round(paper["win_rate"] - float(backtest["win_rate"] or 0), 3) if backtest["win_rate"] is not None else None,
        }
        modeled_bps = None
        costs = (oos.configuration or {}).get("costs") if oos else None
        if costs:
            modeled_bps = (float(costs.get("slippage_rate", 0)) + float(costs.get("spread_bps", 0)) / 20_000) * 10_000
        explanations: list[str] = []
        if modeled_bps is not None and paper["average_slippage_bps"] > modeled_bps * 1.5:
            explanations.append(f"SLIPPAGE_ABOVE_MODEL: paper {paper['average_slippage_bps']}bps vs modeled {round(modeled_bps, 2)}bps")
        backtest_regimes = set()
        if oos and isinstance(metrics.get("regimes"), dict):
            for per_asset in metrics["regimes"].values():
                backtest_regimes |= set(per_asset.keys()) if isinstance(per_asset, dict) else set()
        unseen = [regime for regime in paper["regime_mix"] if regime and backtest_regimes and regime not in backtest_regimes]
        if unseen:
            explanations.append(f"REGIME_NOT_IN_BACKTEST: {unseen}")
        if paper["exit_reasons"].get("stop_loss", 0) > paper["trades"] * 0.6:
            explanations.append("STOP_DOMINATED_EXITS: more than 60% of paper trades exited at the stop")
        if differences["expectancy_pct"] < 0 and not explanations:
            explanations.append("UNEXPLAINED_SHORTFALL: sample variation or changed market conditions; more data required")
        report = DiscrepancyReport(strategy_version_id=version.id, backtest=backtest, paper=paper,
                                   differences=differences, explanations=explanations)
        self.session.add(report)
        self.session.flush()
        return report
