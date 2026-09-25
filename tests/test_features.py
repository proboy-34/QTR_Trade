import numpy as np
import pandas as pd

from app.global_services.features import ema, enrich, sma, vwap


def frame(rows=80):
    close=pd.Series(np.linspace(100,150,rows)+np.sin(np.arange(rows))*2)
    return pd.DataFrame({"timestamp":pd.date_range("2025-01-01",periods=rows,freq="h",tz="UTC"),"open":close-.5,"high":close+2,"low":close-2,"close":close,"volume":1000})

def test_indicators_are_aligned_and_finite_at_end():
    data=frame(); result=enrich(data)
    assert result.iloc[-1].ema_fast > result.iloc[-1].ema_slow
    assert 0 <= result.iloc[-1].rsi <= 100
    assert result.iloc[-1].atr > 0
    assert result.iloc[-1].vwap > 0

def test_sma_ema_and_vwap_values():
    data=frame(30)
    assert sma(data.close,10).iloc[-1] == data.close.tail(10).mean()
    assert ema(data.close,10).iloc[-1] > 0
    assert vwap(data).iloc[-1] > data.low.min()

