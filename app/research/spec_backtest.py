"""Backtest engine for declarative strategy specifications.

Signals are computed at a bar's close and executed at the next bar's open. Stops are
checked before targets when both are touched in one bar (conservative), gaps through a
stop fill at the worse open, and fees, slippage and half the spread apply to both legs.
"""

from dataclasses import asdict, dataclass
from math import sqrt
from typing import Any

import numpy as np
import pandas as pd

from app.global_services.regime import RegimeClassifier
from app.research.dsl import CompiledStrategy, StrategySpec

# Profit factor with no losing trade is reported as a cap (JSON/PostgreSQL cannot store infinity).
PF_CAP = 999.0
BARS_PER_YEAR = {"1m": 525_600, "5m": 105_120, "15m": 35_040, "1h": 8_760, "4h": 2_190, "1d": 365}


@dataclass
class CostModel:
    fee_rate: float = 0.0004
    slippage_rate: float = 0.0002
    spread_bps: float = 2.0

    @property
    def adverse(self) -> float:
        return self.slippage_rate + self.spread_bps / 20_000


@dataclass
class SpecTrade:
    symbol: str
    entry_index: int
    exit_index: int
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    quantity: float
    fees: float
    pnl: float
    return_pct: float
    holding_bars: int
    reason: str
    regime: str


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def summarize(
    trades: list[SpecTrade], curve: np.ndarray, initial_equity: float, timeframe: str,
    bars_in_market: int, total_bars: int, turnover: float,
) -> dict[str, Any]:
    pnls = np.array([trade.pnl for trade in trades], dtype=float)
    trade_returns = np.array([trade.return_pct for trade in trades], dtype=float)
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    peaks = np.maximum.accumulate(curve) if len(curve) else np.array([initial_equity])
    drawdowns = (curve - peaks) / peaks if len(curve) else np.array([0.0])
    returns = np.diff(curve) / curve[:-1] if len(curve) > 1 else np.array([])
    annual = sqrt(BARS_PER_YEAR.get(timeframe, 8_760))
    downside = returns[returns < 0]
    std = float(returns.std()) if len(returns) > 1 else 0.0
    downside_std = float(np.sqrt((downside ** 2).mean())) if len(downside) else 0.0
    final = float(curve[-1]) if len(curve) else initial_equity
    return {
        "initial_equity": initial_equity,
        "final_equity": round(final, 2),
        "total_return": round((final / initial_equity - 1) * 100, 4),
        "net_profit": round(float(pnls.sum()), 2) if len(pnls) else 0.0,
        "number_of_trades": len(trades),
        "win_rate": round(len(wins) / len(pnls) * 100, 2) if len(pnls) else 0.0,
        "average_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "average_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "profit_factor": round(_ratio(float(wins.sum()), abs(float(losses.sum()))), 4) if len(losses) else (
            PF_CAP if len(wins) else 0.0),
        "expectancy": round(float(pnls.mean()), 2) if len(pnls) else 0.0,
        "expectancy_pct": round(float(trade_returns.mean()), 5) if len(trade_returns) else 0.0,
        "maximum_drawdown": round(float(drawdowns.min() * 100), 4) if len(drawdowns) else 0.0,
        "sharpe_ratio": round(float(returns.mean()) / std * annual, 4) if std > 0 else 0.0,
        "sortino_ratio": round(float(returns.mean()) / downside_std * annual, 4) if downside_std > 0 else 0.0,
        "per_bar_sharpe": round(float(returns.mean()) / std, 6) if std > 0 else 0.0,
        "fees_paid": round(float(sum(trade.fees for trade in trades)), 2),
        "turnover": round(turnover, 4),
        "exposure": round(bars_in_market / total_bars, 4) if total_bars else 0.0,
        "average_holding_bars": round(float(np.mean([t.holding_bars for t in trades])), 2) if trades else 0.0,
        "return_skew": round(float(pd.Series(returns).skew()), 4) if len(returns) > 3 else 0.0,
        "return_kurtosis": round(float(pd.Series(returns).kurt()) + 3, 4) if len(returns) > 3 else 3.0,
        "observations": int(len(returns)),
    }


def regime_breakdown(trades: list[SpecTrade]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[SpecTrade]] = {}
    for trade in trades:
        groups.setdefault(trade.regime, []).append(trade)
    result: dict[str, dict[str, Any]] = {}
    for regime, items in groups.items():
        pnls = np.array([item.pnl for item in items])
        wins, losses = pnls[pnls > 0], pnls[pnls < 0]
        result[regime] = {
            "trades": len(items),
            "win_rate": round(len(wins) / len(items) * 100, 2),
            "average_return_pct": round(float(np.mean([item.return_pct for item in items])), 5),
            "profit_factor": round(_ratio(float(wins.sum()), abs(float(losses.sum()))), 4) if len(losses) else None,
            "evidence": "sufficient" if len(items) >= 5 else "insufficient",
        }
    return result


class SpecBacktestEngine:
    def run(
        self, spec: StrategySpec, candles: pd.DataFrame, *, symbol: str = "", timeframe: str = "1h",
        costs: CostModel | None = None, initial_equity: float = 100_000, risk_fraction: float = 0.01,
        trade_from: int = 0, regimes: pd.Series | None = None,
    ) -> dict[str, Any]:
        """Run on ``candles``; entries are only allowed at or after row ``trade_from`` (warm-up)."""
        costs = costs or CostModel()
        data = candles.sort_values("timestamp").reset_index(drop=True)
        for column in ("open", "high", "low", "close", "volume"):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        compiled = CompiledStrategy(spec)
        if len(data) < compiled.warmup() + 2:
            raise ValueError(f"At least {compiled.warmup() + 2} candles are required")
        signals = compiled.signals(data)
        labels = regimes.reset_index(drop=True) if regimes is not None else RegimeClassifier().classify_series(data)
        allowed_regimes = set(spec.regimes)
        stop_pct, target_pct = spec.risk.stop_loss_pct, spec.risk.take_profit_pct
        equity = initial_equity
        curve = [equity]
        trades: list[SpecTrade] = []
        position: dict[str, Any] | None = None
        bars_in_market = 0
        turnover = 0.0
        opens, highs, lows = data["open"].to_numpy(), data["high"].to_numpy(), data["low"].to_numpy()
        stamps = data["timestamp"].astype(str).to_numpy()
        for index in range(1, len(data)):
            prior = index - 1
            if position is None:
                regime_ok = not allowed_regimes or labels.iloc[prior] in allowed_regimes
                if prior >= trade_from and bool(signals["entry"].iloc[prior]) and regime_ok:
                    entry = float(opens[index]) * (1 + costs.adverse)
                    stop = entry * (1 - stop_pct)
                    quantity = min(equity * risk_fraction / (entry - stop), equity / entry)
                    fee = entry * quantity * costs.fee_rate
                    position = {"index": index, "entry": entry, "stop": stop, "target": entry * (1 + target_pct),
                                "quantity": quantity, "fee": fee, "regime": str(labels.iloc[prior])}
                    turnover += entry * quantity / initial_equity
            if position is not None:
                bars_in_market += 1
                exit_price: float | None = None
                reason = ""
                if lows[index] <= position["stop"]:
                    exit_price = min(float(opens[index]), position["stop"]) if index > position["index"] else position["stop"]
                    reason = "stop_loss"
                elif highs[index] >= position["target"]:
                    exit_price = max(float(opens[index]), position["target"]) if index > position["index"] else position["target"]
                    reason = "take_profit"
                elif index > position["index"] and bool(signals["exit"].iloc[prior]):
                    exit_price, reason = float(opens[index]), "signal"
                elif spec.risk.max_holding_bars and index - position["index"] >= spec.risk.max_holding_bars:
                    exit_price, reason = float(data["close"].iloc[index]), "max_holding"
                if exit_price is not None:
                    exit_fill = exit_price * (1 - costs.adverse)
                    exit_fee = exit_fill * position["quantity"] * costs.fee_rate
                    fees = position["fee"] + exit_fee
                    pnl = (exit_fill - position["entry"]) * position["quantity"] - fees
                    turnover += exit_fill * position["quantity"] / initial_equity
                    trades.append(SpecTrade(
                        symbol, position["index"], index, stamps[position["index"]], stamps[index],
                        round(position["entry"], 8), round(exit_fill, 8), position["quantity"], fees, pnl,
                        pnl / (position["entry"] * position["quantity"]) * 100, index - position["index"] + 1,
                        reason, position["regime"],
                    ))
                    equity += pnl
                    position = None
            marked = equity
            if position is not None:
                marked += (float(data["close"].iloc[index]) - position["entry"]) * position["quantity"] - position["fee"]
            curve.append(marked)
        metrics = summarize(trades, np.array(curve), initial_equity, timeframe, bars_in_market,
                            max(1, len(data) - 1 - trade_from), turnover)
        return {
            "metrics": metrics,
            "trades": [asdict(trade) for trade in trades],
            "regimes": regime_breakdown(trades),
            "equity_curve": [{"timestamp": stamps[i], "equity": round(value, 2)} for i, value in enumerate(curve)][:: max(1, len(curve) // 500)],
            "assumptions": {"execution": "next_bar_open", "stop_before_target": True, "costs": asdict(costs),
                            "risk_fraction": risk_fraction, "warmup_bars": trade_from},
        }
