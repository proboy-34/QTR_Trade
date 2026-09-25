"""Portfolio-level risk: correlation, concentration, beta and correlated-cluster exposure."""

from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.decimal_math import decimal
from app.models import MarketCandle, PortfolioSnapshot, Position

OPEN = ("OPENING", "OPEN", "MANAGING", "PARTIALLY_CLOSING", "CLOSING")


class PortfolioIntelligence:
    def __init__(self, session: Session, timeframe: str = "1h", lookback: int = 200) -> None:
        self.session = session
        self.timeframe = timeframe
        self.lookback = lookback

    def _returns(self, symbol: str) -> pd.Series | None:
        exchange = self.session.scalar(
            select(MarketCandle.exchange).where(MarketCandle.symbol == symbol, MarketCandle.timeframe == self.timeframe)
            .group_by(MarketCandle.exchange).order_by(desc(func.count())).limit(1)
        )
        if not exchange:
            return None
        rows = self.session.execute(select(MarketCandle.timestamp, MarketCandle.close).where(
            MarketCandle.exchange == exchange, MarketCandle.symbol == symbol, MarketCandle.timeframe == self.timeframe,
        ).order_by(desc(MarketCandle.timestamp)).limit(self.lookback + 1)).all()
        if len(rows) < 30:
            return None
        series = pd.Series([float(row[1]) for row in reversed(rows)],
                           index=pd.to_datetime([row[0] for row in reversed(rows)], utc=True))
        return series.pct_change().dropna()

    def exposures(self) -> tuple[dict[str, float], float]:
        latest = self.session.scalar(select(PortfolioSnapshot).order_by(PortfolioSnapshot.captured_at.desc()))
        equity = float(latest.equity) if latest else 0.0
        notional: dict[str, float] = {}
        for position in self.session.scalars(select(Position).where(Position.status.in_(OPEN))).all():
            sign = 1 if position.side == "BUY" else -1
            notional[position.symbol] = notional.get(position.symbol, 0.0) + sign * float(
                decimal(position.quantity) * decimal(position.current_price))
        return notional, equity

    def correlation(self, symbols: list[str]) -> pd.DataFrame:
        series = {symbol: returns for symbol in symbols if (returns := self._returns(symbol)) is not None}
        if len(series) < 2:
            return pd.DataFrame(index=list(series), columns=list(series), dtype=float)
        return pd.DataFrame(series).dropna().corr()

    def analyze(self) -> dict[str, Any]:
        notional, equity = self.exposures()
        symbols = sorted(notional)
        gross = sum(abs(value) for value in notional.values())
        weights = {symbol: abs(value) / gross for symbol, value in notional.items()} if gross else {}
        hhi = sum(weight**2 for weight in weights.values())
        matrix = self.correlation(sorted({*symbols, "BTCUSDT"}))
        betas: dict[str, float | None] = {}
        btc = self._returns("BTCUSDT")
        for symbol in symbols:
            asset = self._returns(symbol)
            if asset is None or btc is None:
                betas[symbol] = None
                continue
            joined = pd.concat([asset, btc], axis=1, join="inner").dropna()
            variance = float(joined.iloc[:, 1].var())
            betas[symbol] = round(float(joined.iloc[:, 0].cov(joined.iloc[:, 1]) / variance), 4) if variance > 0 and len(joined) > 10 else None
        portfolio_beta = sum((notional[s] / equity) * (betas[s] or 0) for s in symbols) if equity else 0.0
        return {
            "equity": equity, "gross_exposure": round(gross / equity, 6) if equity else 0.0,
            "net_exposure": round(sum(notional.values()) / equity, 6) if equity else 0.0,
            "positions": {symbol: round(value, 4) for symbol, value in notional.items()},
            "weights": {symbol: round(value, 4) for symbol, value in weights.items()},
            "concentration_hhi": round(hhi, 4),
            "effective_bets": round(1 / hhi, 3) if hhi else 0.0,
            "betas_to_btc": betas, "portfolio_beta_to_btc": round(portfolio_beta, 4),
            "correlation": {row: {col: (None if pd.isna(value) else round(float(value), 4)) for col, value in values.items()}
                            for row, values in matrix.to_dict().items()},
            "note": "Correlations use recent returns and can change abruptly in stress regimes.",
        }

    def assess_candidate(self, symbol: str, notional: float, equity: float, *, threshold: float,
                         max_cluster: float, max_symbol: float) -> dict[str, Any]:
        positions, _ = self.exposures()
        reasons: list[str] = []
        symbol_exposure = (abs(positions.get(symbol, 0.0)) + notional) / equity if equity > 0 else 1.0
        if symbol_exposure > max_symbol:
            reasons.append("SYMBOL_CONCENTRATION_LIMIT")
        others = [item for item in positions if item != symbol]
        cluster = [symbol]
        correlations: dict[str, float] = {}
        if others:
            matrix = self.correlation([symbol, *others])
            if symbol in matrix.columns:
                for other in others:
                    if other in matrix.columns and pd.notna(matrix.loc[symbol, other]):
                        correlations[other] = round(float(matrix.loc[symbol, other]), 4)
                        if float(matrix.loc[symbol, other]) >= threshold:
                            cluster.append(other)
        cluster_exposure = (notional + sum(abs(positions[item]) for item in cluster if item in positions)) / equity if equity > 0 else 1.0
        if cluster_exposure > max_cluster and len(cluster) > 1:
            reasons.append("CORRELATED_EXPOSURE_LIMIT")
        return {"reasons": reasons, "symbol_exposure": round(symbol_exposure, 6),
                "cluster": cluster, "cluster_exposure": round(cluster_exposure, 6), "correlations": correlations,
                "correlation_data": bool(correlations) or not others}


def correlation_stats(matrix: pd.DataFrame) -> float | None:
    if matrix.shape[0] < 2:
        return None
    values = matrix.to_numpy()
    upper = values[np.triu_indices_from(values, k=1)]
    upper = upper[~np.isnan(upper)]
    return round(float(upper.mean()), 4) if len(upper) else None
