import numpy as np
import pandas as pd

from app.global_services.features import enrich


class ResearchAnalyzer:
    def analyze(self, candles: pd.DataFrame) -> dict:
        data = enrich(candles)
        returns = data["close"].pct_change()
        rolling_mean = returns.rolling(20).mean()
        rolling_std = returns.rolling(20).std()
        anomalies = data.index[((returns - rolling_mean).abs() > 3 * rolling_std).fillna(False)].tolist()
        breakouts = data.index[
            (data["close"] > data["high"].shift(1).rolling(20).max()).fillna(False)
        ].tolist()
        swings_high = data.index[
            ((data["high"] > data["high"].shift(1)) & (data["high"] > data["high"].shift(-1))).fillna(False)
        ].tolist()
        numeric = data.select_dtypes(include=np.number)
        return {
            "statistics": {
                "mean_return": float(returns.mean()),
                "variance": float(returns.var()),
                "standard_deviation": float(returns.std()),
                "return_quantiles": {str(key): float(value) for key, value in returns.quantile([0.05, 0.5, 0.95]).items()},
            },
            "correlations": numeric[[column for column in ("close", "volume", "rsi", "atr", "momentum", "volatility") if column in numeric]].corr().fillna(0).to_dict(),
            "patterns": {"breakouts": breakouts, "swing_highs": swings_high},
            "anomalies": anomalies,
            "insights": [
                f"Latest deterministic regime: {data.iloc[-1]['regime']}",
                f"Detected {len(breakouts)} close breakouts and {len(anomalies)} return anomalies",
                "Observed relationships are correlations and are not evidence of causality",
            ],
        }

