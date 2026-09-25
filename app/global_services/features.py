import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + relative_strength)).fillna(50)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def vwap(frame: pd.DataFrame) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3
    return (typical * frame["volume"]).cumsum() / frame["volume"].cumsum().replace(0, np.nan)


def enrich(frame: pd.DataFrame, fast: int = 10, slow: int = 30) -> pd.DataFrame:
    result = frame.copy()
    result["ema_fast"] = ema(result["close"], fast)
    result["ema_slow"] = ema(result["close"], slow)
    result["sma_20"] = sma(result["close"], 20)
    result["rsi"] = rsi(result["close"])
    result["atr"] = atr(result)
    result["vwap"] = vwap(result)
    result["momentum"] = result["close"].pct_change(10)
    result["volatility"] = result["close"].pct_change().rolling(20).std() * np.sqrt(365 * 24)
    result["trend"] = np.where(result["ema_fast"] > result["ema_slow"], "bullish", "bearish")
    spread = (result["ema_fast"] - result["ema_slow"]).abs() / result["close"]
    result["regime"] = np.where(spread < 0.002, "sideways", "trending")
    return result

