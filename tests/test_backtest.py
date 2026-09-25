import numpy as np
import pandas as pd

from app.global_services.backtest import BacktestEngine


def test_backtest_returns_auditable_metrics_and_curve():
    n=180; close=100+np.linspace(0,15,n)+np.sin(np.arange(n)/7)*8
    data=pd.DataFrame({"timestamp":pd.date_range("2025-01-01",periods=n,freq="h",tz="UTC"),"open":close,"high":close+2,"low":close-2,"close":close,"volume":1000})
    result=BacktestEngine().run(data,fast=5,slow=15)
    assert len(result["equity_curve"]) == n
    assert "maximum_drawdown" in result["metrics"]
    assert result["metrics"]["number_of_trades"] == len(result["trades"])

