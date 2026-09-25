import numpy as np
import pandas as pd

from app.global_services.backtest import BacktestEngine


def oscillating_frame(rows: int = 180) -> pd.DataFrame:
    close = 100 + np.sin(np.arange(rows) / 5) * 12 + np.linspace(0, 8, rows)
    opens = close + np.cos(np.arange(rows)) * 0.7
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="h", tz="UTC"),
        "open": opens,
        "high": np.maximum(opens, close) + 2,
        "low": np.minimum(opens, close) - 2,
        "close": close,
        "volume": 1000,
    })


def test_entry_uses_next_bar_open_not_signal_close():
    data = oscillating_frame()
    slippage = 0.001
    result = BacktestEngine().run(data, fast=3, slow=9, slippage_rate=slippage)
    assert result["trades"]
    trade = result["trades"][0]
    entry_index = data.index[data.timestamp.astype(str) == trade["entry_time"]][0]
    assert trade["entry_price"] == data.iloc[entry_index].open * (1 + slippage)


def test_input_order_is_normalized_without_mutating_source():
    data = oscillating_frame()
    reversed_data = data.iloc[::-1].reset_index(drop=True)
    original_first = reversed_data.iloc[0].timestamp
    result = BacktestEngine().run(reversed_data, fast=3, slow=9)
    assert result["equity_curve"][0]["timestamp"] == str(data.iloc[0].timestamp)
    assert reversed_data.iloc[0].timestamp == original_first


def test_stop_wins_when_stop_and_target_touch_same_candle():
    data = oscillating_frame()
    baseline = BacktestEngine().run(
        data, fast=3, slow=9, stop_loss_pct=0.005, take_profit_pct=0.005
    )
    entry_index = data.index[data.timestamp.astype(str) == baseline["trades"][0]["entry_time"]][0]
    data.loc[entry_index + 1, "low"] = 1
    data.loc[entry_index + 1, "high"] = 1_000
    result = BacktestEngine().run(
        data, fast=3, slow=9, stop_loss_pct=0.005, take_profit_pct=0.005
    )
    assert result["trades"][0]["reason"] == "stop_loss"
