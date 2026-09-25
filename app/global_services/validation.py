from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.global_services.backtest import BacktestEngine
from app.models import ValidationResult


@dataclass
class WalkForwardConfig:
    train_size: int = 100
    validation_size: int = 40
    step_size: int = 40
    min_profit_factor: float = 1.0
    max_drawdown_pct: float = -20.0


class WalkForwardValidator:
    def __init__(self, engine: BacktestEngine | None = None) -> None:
        self.engine = engine or BacktestEngine()

    def run(self, candles: pd.DataFrame, config: WalkForwardConfig, **backtest: Any) -> dict:
        windows = []
        start = 0
        while start + config.train_size + config.validation_size <= len(candles):
            train_end = start + config.train_size
            validation_end = train_end + config.validation_size
            training = candles.iloc[start:train_end].copy()
            out_of_sample = candles.iloc[train_end:validation_end].copy()
            train_result = self.engine.run(training, **backtest)
            validation_result = self.engine.run(out_of_sample, **backtest)
            windows.append({
                "training": {"start": str(training.iloc[0].timestamp), "end": str(training.iloc[-1].timestamp), "metrics": train_result["metrics"]},
                "out_of_sample": {"start": str(out_of_sample.iloc[0].timestamp), "end": str(out_of_sample.iloc[-1].timestamp), "metrics": validation_result["metrics"]},
            })
            start += config.step_size
        if not windows:
            raise ValueError("Insufficient candles for a walk-forward window")
        oos = [window["out_of_sample"]["metrics"] for window in windows]
        aggregate = {
            "windows": len(windows),
            "average_oos_return": sum(item["total_return"] for item in oos) / len(oos),
            "worst_oos_drawdown": min(item["maximum_drawdown"] for item in oos),
            "average_oos_profit_factor": sum(item["profit_factor"] for item in oos) / len(oos),
            "oos_trades": sum(item["number_of_trades"] for item in oos),
        }
        passed = (
            aggregate["average_oos_profit_factor"] >= config.min_profit_factor
            and aggregate["worst_oos_drawdown"] >= config.max_drawdown_pct
            and aggregate["oos_trades"] > 0
        )
        return {"result": "PASS" if passed else "FAIL", "aggregate": aggregate, "windows": windows}

    def persist(
        self,
        session: Session,
        strategy_version_id: str,
        dataset_id: str | None,
        result: dict,
        config: WalkForwardConfig,
    ) -> ValidationResult:
        record = ValidationResult(
            strategy_version_id=strategy_version_id,
            dataset_id=dataset_id,
            method="walk_forward",
            result=result["result"],
            metrics={"aggregate": result["aggregate"], "windows": result["windows"]},
            rules={"min_profit_factor": config.min_profit_factor, "max_drawdown_pct": config.max_drawdown_pct},
            configuration={"train_size": config.train_size, "validation_size": config.validation_size, "step_size": config.step_size},
            notes="Rolling out-of-sample validation",
        )
        session.add(record)
        session.commit()
        return record
