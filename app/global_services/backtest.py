from dataclasses import asdict, dataclass
from math import sqrt

import numpy as np
import pandas as pd

from app.global_services.features import enrich


@dataclass
class BacktestTrade:
    entry_time: str
    exit_time: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    fee: float
    pnl: float
    reason: str


class BacktestEngine:
    """Long-only EMA crossover engine; signals execute on the next candle open."""

    def run(
        self,
        candles: pd.DataFrame,
        *,
        initial_equity: float = 100_000,
        fast: int = 10,
        slow: int = 30,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        risk_fraction: float = 0.01,
        fee_rate: float = 0.0004,
        slippage_rate: float = 0.0002,
    ) -> dict:
        if len(candles) < slow + 2:
            raise ValueError(f"At least {slow + 2} candles are required")
        data = enrich(candles.sort_values("timestamp").reset_index(drop=True), fast, slow)
        data["entry_signal"] = (data.ema_fast > data.ema_slow) & (
            data.ema_fast.shift(1) <= data.ema_slow.shift(1)
        )
        data["exit_signal"] = (data.ema_fast < data.ema_slow) & (
            data.ema_fast.shift(1) >= data.ema_slow.shift(1)
        )
        equity = initial_equity
        equity_curve = [{"timestamp": str(data.iloc[0].timestamp), "equity": equity}]
        trades: list[BacktestTrade] = []
        position: dict | None = None
        for index in range(1, len(data)):
            previous = data.iloc[index - 1]
            candle = data.iloc[index]
            if position is None and bool(previous.entry_signal):
                entry = float(candle.open) * (1 + slippage_rate)
                risk_amount = equity * risk_fraction
                quantity = risk_amount / (entry * stop_loss_pct)
                position = {
                    "entry_time": str(candle.timestamp),
                    "entry": entry,
                    "quantity": quantity,
                    "stop": entry * (1 - stop_loss_pct),
                    "target": entry * (1 + take_profit_pct),
                }
            elif position is not None:
                exit_price: float | None = None
                reason = ""
                if float(candle.low) <= position["stop"]:
                    exit_price, reason = position["stop"] * (1 - slippage_rate), "stop_loss"
                elif float(candle.high) >= position["target"]:
                    exit_price, reason = position["target"] * (1 - slippage_rate), "take_profit"
                elif bool(previous.exit_signal):
                    exit_price, reason = float(candle.open) * (1 - slippage_rate), "signal"
                if exit_price is not None:
                    notional = (position["entry"] + exit_price) * position["quantity"]
                    fee = notional * fee_rate
                    pnl = (exit_price - position["entry"]) * position["quantity"] - fee
                    equity += pnl
                    trades.append(
                        BacktestTrade(
                            position["entry_time"], str(candle.timestamp), "BUY",
                            position["entry"], exit_price, position["quantity"], fee, pnl, reason,
                        )
                    )
                    position = None
            equity_curve.append({"timestamp": str(candle.timestamp), "equity": equity})
        pnls = np.array([trade.pnl for trade in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        curve = np.array([point["equity"] for point in equity_curve])
        peaks = np.maximum.accumulate(curve)
        drawdowns = (curve - peaks) / peaks
        returns = pd.Series(curve).pct_change().dropna()
        metrics = {
            "initial_equity": initial_equity,
            "final_equity": round(float(equity), 2),
            "total_return": round((equity / initial_equity - 1) * 100, 4),
            "net_profit": round(float(pnls.sum()) if len(pnls) else 0, 2),
            "number_of_trades": len(trades),
            "win_rate": round(float(len(wins) / len(pnls) * 100), 2) if len(pnls) else 0,
            "average_win": round(float(wins.mean()), 2) if len(wins) else 0,
            "average_loss": round(float(losses.mean()), 2) if len(losses) else 0,
            "profit_factor": round(float(wins.sum() / abs(losses.sum())), 3) if len(losses) else 0,
            "maximum_drawdown": round(float(drawdowns.min() * 100), 3),
            "expectancy": round(float(pnls.mean()), 2) if len(pnls) else 0,
            "sharpe_ratio": round(float(returns.mean() / returns.std() * sqrt(365 * 24)), 3)
            if len(returns) > 1 and returns.std() > 0 else 0,
        }
        return {"metrics": metrics, "trades": [asdict(t) for t in trades], "equity_curve": equity_curve}

