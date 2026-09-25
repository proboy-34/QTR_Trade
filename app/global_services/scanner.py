"""Multi-asset market scanner.

The scanner observes persisted closed candles, detects unusual conditions, updates
regime/market memory and creates or refreshes Opportunity records. It never trades:
opportunities are evidence for Decision and Research, not orders.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.core.time import TimeService
from app.global_services.features import enrich
from app.global_services.regime import RegimeService, load_candles
from app.memory.market_memory import MarketMemoryService, context_features
from app.models import (
    AssetEligibility,
    MarketDataValidationFailure,
    MarketEvent,
    Opportunity,
    Strategy,
)

OPEN_STATUSES = ("DETECTED", "QUEUED", "EVALUATING")


@dataclass
class Signal:
    code: str
    strength: float
    detail: dict[str, Any] = field(default_factory=dict)


class SignalDetector:
    """Pure detectors over a closed-candle frame. Thresholds are explicit and reviewable."""

    def __init__(
        self, volume_spike: float = 2.5, volatility_expansion: float = 1.6,
        abnormal_z: float = 3.0, breakout_lookback: int = 20,
    ) -> None:
        self.volume_spike = volume_spike
        self.volatility_expansion = volatility_expansion
        self.abnormal_z = abnormal_z
        self.breakout_lookback = breakout_lookback

    def detect(self, frame: pd.DataFrame, reference: pd.DataFrame | None = None) -> list[Signal]:
        if len(frame) < max(60, self.breakout_lookback + 5):
            return []
        data = frame.sort_values("timestamp").reset_index(drop=True)
        close, high, low, volume = data["close"], data["high"], data["low"], data["volume"]
        returns = close.pct_change()
        signals: list[Signal] = []
        baseline_volume = volume.shift(1).rolling(20).median().iloc[-1]
        if baseline_volume and baseline_volume > 0:
            ratio = float(volume.iloc[-1] / baseline_volume)
            if ratio >= self.volume_spike:
                signals.append(Signal("VOLUME_SPIKE", min(1.0, ratio / (self.volume_spike * 2)), {"ratio": round(ratio, 3)}))
            recent_sum, prior_sum = volume.iloc[-24:].sum(), volume.iloc[-48:-24].sum()
            if len(volume) >= 48 and prior_sum > 0 and recent_sum / prior_sum < 0.4:
                signals.append(Signal("LIQUIDITY_DROP", 0.5, {"ratio": round(float(recent_sum / prior_sum), 3)}))
        short_vol, long_vol = returns.iloc[-10:].std(), returns.iloc[-60:-10].std()
        if long_vol and long_vol > 0 and np.isfinite(short_vol):
            expansion = float(short_vol / long_vol)
            if expansion >= self.volatility_expansion:
                signals.append(Signal("VOLATILITY_EXPANSION", min(1.0, expansion / 3), {"ratio": round(expansion, 3)}))
        window_high = high.shift(1).rolling(self.breakout_lookback).max().iloc[-1]
        window_low = low.shift(1).rolling(self.breakout_lookback).min().iloc[-1]
        last = float(close.iloc[-1])
        if np.isfinite(window_high) and last > window_high:
            signals.append(Signal("BREAKOUT_UP", 0.7, {"level": float(window_high), "close": last}))
        elif np.isfinite(window_low) and last < window_low:
            signals.append(Signal("BREAKOUT_DOWN", 0.7, {"level": float(window_low), "close": last}))
        sigma = returns.iloc[-51:-1].std()
        if sigma and sigma > 0 and np.isfinite(returns.iloc[-1]):
            z = float(returns.iloc[-1] / sigma)
            if abs(z) >= self.abnormal_z:
                signals.append(Signal("ABNORMAL_MOVE", min(1.0, abs(z) / 6), {"z_score": round(z, 3)}))
        enriched = enrich(data[["timestamp", "open", "high", "low", "close", "volume"]])
        fast, slow = enriched["ema_fast"], enriched["ema_slow"]
        if len(fast) > 2 and pd.notna(fast.iloc[-2]) and pd.notna(slow.iloc[-2]):
            if fast.iloc[-1] > slow.iloc[-1] and fast.iloc[-2] <= slow.iloc[-2]:
                signals.append(Signal("TREND_CHANGE_UP", 0.5, {"ema_fast": float(fast.iloc[-1])}))
            elif fast.iloc[-1] < slow.iloc[-1] and fast.iloc[-2] >= slow.iloc[-2]:
                signals.append(Signal("TREND_CHANGE_DOWN", 0.5, {"ema_fast": float(fast.iloc[-1])}))
        momentum = close.pct_change(10)
        if len(momentum.dropna()) > 2 and np.sign(momentum.iloc[-1]) != np.sign(momentum.iloc[-2]):
            magnitude = abs(float(momentum.iloc[-1]))
            if magnitude > 2 * float(returns.iloc[-60:].std() or 0):
                signals.append(Signal("MOMENTUM_SHIFT", 0.4, {"momentum_10": round(float(momentum.iloc[-1]), 5)}))
        if reference is not None and len(reference) >= 100:
            joined = pd.merge(
                data[["timestamp", "close"]], reference[["timestamp", "close"]], on="timestamp", suffixes=("", "_ref")
            )
            if len(joined) >= 100:
                a, b = joined["close"].pct_change(), joined["close_ref"].pct_change()
                recent, prior = a.iloc[-50:].corr(b.iloc[-50:]), a.iloc[-100:-50].corr(b.iloc[-100:-50])
                if np.isfinite(recent) and np.isfinite(prior) and abs(recent - prior) >= 0.4:
                    signals.append(Signal("CORRELATION_CHANGE", 0.4, {"recent": round(float(recent), 3), "prior": round(float(prior), 3)}))
        return signals


class OpportunityRanker:
    """Explainable priority for research/decision attention. Not a return forecast."""

    weights = {"liquidity": 0.2, "data_quality": 0.15, "regime_clarity": 0.15, "signal_strength": 0.2,
               "strategy_applicability": 0.15, "news_relevance": 0.1, "risk": 0.05}

    def rank(self, *, quote_volume: float | None, quality_failures: int, regime_confidence: float,
             signals: list[Signal], applicable_strategies: int, news_relevance: float,
             volatility: float | None) -> tuple[float, dict[str, Any]]:
        components = {
            "liquidity": min(1.0, np.log10(max(quote_volume or 1, 1)) / 10),
            "data_quality": 1.0 if quality_failures == 0 else max(0.0, 1 - quality_failures / 5),
            "regime_clarity": float(regime_confidence),
            "signal_strength": min(1.0, sum(item.strength for item in signals) / 2),
            "strategy_applicability": min(1.0, applicable_strategies / 2),
            "news_relevance": min(1.0, news_relevance),
            # Extremely volatile markets are penalised: risk of adverse fills and gaps.
            "risk": 1.0 - min(1.0, (volatility or 0) / 0.05),
        }
        score = sum(self.weights[key] * value for key, value in components.items())
        return round(score * 100, 2), {
            "components": {key: round(value, 4) for key, value in components.items()},
            "weights": self.weights,
            "note": "Evidence-based attention priority; not a prediction of return.",
        }


class MarketScanner:
    def __init__(self, session: Session, settings: Settings, event_bus: EventBus) -> None:
        self.session = session
        self.settings = settings
        self.event_bus = event_bus
        self.detector = SignalDetector()
        self.ranker = OpportunityRanker()

    async def scan(self, exchange: str, timeframe: str, symbols: list[str]) -> dict[str, Any]:
        reference = load_candles(self.session, exchange, "BTCUSDT", timeframe, 300)
        frames = {symbol: load_candles(self.session, exchange, symbol, timeframe, 300) for symbol in symbols}
        last_returns = {
            symbol: float(frame["close"].pct_change().iloc[-1]) / float(frame["close"].pct_change().iloc[-51:-1].std() or 1)
            for symbol, frame in frames.items() if len(frame) >= 60
        }
        market_wide = self._market_wide(last_returns)
        results: list[dict[str, Any]] = []
        for symbol, frame in frames.items():
            if len(frame) < 60:
                results.append({"symbol": symbol, "status": "INSUFFICIENT_HISTORY", "bars": len(frame)})
                continue
            results.append(await self._scan_symbol(exchange, symbol, timeframe, frame, reference, market_wide))
        self.session.commit()
        return {
            "exchange": exchange, "timeframe": timeframe, "scanned": len(symbols),
            "market_wide": market_wide, "results": results,
            "opportunities": sum(1 for item in results if item.get("opportunity_id")),
        }

    @staticmethod
    def _market_wide(z_scores: dict[str, float]) -> dict[str, Any] | None:
        if len(z_scores) < 5:
            return None
        values = np.array([value for value in z_scores.values() if np.isfinite(value)])
        up, down = float((values > 2).mean()), float((values < -2).mean())
        if max(up, down) >= 0.6:
            return {"direction": "UP" if up > down else "DOWN", "share": round(max(up, down), 3), "assets": len(values)}
        return None

    async def _scan_symbol(
        self, exchange: str, symbol: str, timeframe: str, frame: pd.DataFrame,
        reference: pd.DataFrame, market_wide: dict[str, Any] | None,
    ) -> dict[str, Any]:
        candle_at = TimeService.ensure_utc(pd.Timestamp(frame["timestamp"].iloc[-1]).to_pydatetime())
        regime_record, regime = RegimeService(self.session).update(exchange, symbol, timeframe, frame)
        if regime_record is not None and regime_record.is_transition and regime_record.candle_timestamp == candle_at:
            await publish_persisted(self.session, self.event_bus, Event("REGIME_CHANGED", {
                "exchange": exchange, "symbol": symbol, "timeframe": timeframe,
                "from": regime_record.previous_regime, "to": regime_record.regime,
                "regime_record_id": regime_record.id,
            }, source="regime_engine"), "regime_engine")
        features = context_features(frame)
        enriched_last = enrich(frame).iloc[-1]
        context = MarketMemoryService(self.session).remember(
            exchange=exchange, symbol=symbol, timeframe=timeframe, observed_at=candle_at,
            price=frame["close"].iloc[-1], features=features, regime=regime.regime,
            regime_confidence=regime.confidence, legacy_regime=str(enriched_last["regime"]),
            direction=str(enriched_last["trend"]).upper(), extra={"regime_evidence": regime.evidence},
        )
        signals = self.detector.detect(frame, None if symbol == "BTCUSDT" else reference)
        if regime_record is not None and regime_record.is_transition:
            signals.append(Signal("REGIME_TRANSITION", 0.5, {"from": regime_record.previous_regime, "to": regime.regime}))
        if market_wide:
            signals.append(Signal("MARKET_WIDE_MOVE", 0.3, market_wide))
        if not signals:
            return {"symbol": symbol, "status": "NO_SIGNAL", "regime": regime.regime, "context_id": context.id}
        events, relevance = self._events(symbol, candle_at)
        eligibility = self.session.scalar(select(AssetEligibility).where(
            AssetEligibility.exchange == exchange, AssetEligibility.symbol == symbol,
        ).order_by(AssetEligibility.evaluated_at.desc()))
        failures = len(self.session.scalars(select(MarketDataValidationFailure.id).where(
            MarketDataValidationFailure.exchange == exchange, MarketDataValidationFailure.symbol == symbol,
            MarketDataValidationFailure.created_at >= TimeService.now() - timedelta(days=1),
        )).all())
        applicable = self.applicable_strategies(symbol, timeframe, regime.regime)
        score, breakdown = self.ranker.rank(
            quote_volume=(eligibility.metrics or {}).get("quote_volume_24h") if eligibility else None,
            quality_failures=failures, regime_confidence=regime.confidence, signals=signals,
            applicable_strategies=len(applicable), news_relevance=relevance,
            volatility=features.get("volatility"),
        )
        breakdown["applicable_strategy_ids"] = applicable
        opportunity, created = self._upsert(
            exchange, symbol, timeframe, candle_at, context.id, signals, regime.regime,
            score, breakdown, [event.id for event in events],
        )
        if created:
            await publish_persisted(self.session, self.event_bus, Event("OPPORTUNITY_CREATED", {
                "opportunity_id": opportunity.id, "symbol": symbol, "timeframe": timeframe,
                "signals": [item.code for item in signals], "rank_score": score,
            }, source="market_scanner"), "market_scanner")
        return {
            "symbol": symbol, "status": "OPPORTUNITY", "opportunity_id": opportunity.id,
            "created": created, "signals": [item.code for item in signals], "regime": regime.regime,
            "rank_score": score,
        }

    def _events(self, symbol: str, at: datetime) -> tuple[list[MarketEvent], float]:
        base = symbol.removesuffix("USDT").removesuffix("USDC")
        rows = self.session.scalars(select(MarketEvent).where(
            MarketEvent.event_at >= at - timedelta(hours=24), MarketEvent.event_at <= at + timedelta(hours=24),
        ).order_by(MarketEvent.event_at.desc()).limit(200)).all()
        related = [row for row in rows if base in (row.affected_assets or []) or symbol in (row.affected_assets or [])]
        relevance = max((float(row.relevance or 0) for row in related), default=0.0)
        return related, relevance

    def applicable_strategies(self, symbol: str, timeframe: str, regime: str) -> list[str]:
        strategies = self.session.scalars(select(Strategy).where(
            Strategy.status.in_(["active", "paper_testing"]), Strategy.timeframe == timeframe,
        )).all()
        result: list[str] = []
        for strategy in strategies:
            if not strategy.versions:
                continue
            version = max(strategy.versions, key=lambda item: item.version)
            filters = version.filters or {}
            universe = [item.upper() for item in filters.get("universe", [])]
            covers = strategy.symbol == symbol or symbol in universe or "ELIGIBLE" in universe
            regimes = filters.get("market_regimes")
            if covers and (not regimes or regime in regimes):
                result.append(strategy.id)
        return result

    def _upsert(
        self, exchange: str, symbol: str, timeframe: str, candle_at: datetime, context_id: str,
        signals: list[Signal], regime: str, score: float, breakdown: dict[str, Any], event_ids: list[str],
    ) -> tuple[Opportunity, bool]:
        now = TimeService.now()
        payload = [{"code": item.code, "strength": round(item.strength, 3), **item.detail} for item in signals]
        existing = self.session.scalar(select(Opportunity).where(
            Opportunity.source == "scanner", Opportunity.exchange == exchange, Opportunity.symbol == symbol,
            Opportunity.timeframe == timeframe, Opportunity.status.in_(OPEN_STATUSES),
            Opportunity.expires_at > now,
        ).order_by(Opportunity.created_at.desc()))
        dedup_key = f"{exchange}:{symbol}:{timeframe}:{candle_at.isoformat()}"
        if existing:
            if existing.dedup_key != dedup_key:
                existing.observations = (existing.observations or 1) + 1
            existing.signals, existing.regime, existing.rank_score = payload, regime, score
            existing.rank_breakdown, existing.event_ids = breakdown, event_ids
            existing.market_context_id, existing.dedup_key, existing.last_seen_at = context_id, dedup_key, now
            existing.reasons = [item.code for item in signals]
            existing.expires_at = now + timedelta(minutes=self.settings.opportunity_ttl_minutes)
            return existing, False
        opportunity = Opportunity(
            market_context_id=context_id, strategy_version_id=None, symbol=symbol, status="DETECTED",
            score=score, reasons=[item.code for item in signals], source="scanner", exchange=exchange,
            timeframe=timeframe, signals=payload, regime=regime, dedup_key=dedup_key, rank_score=score,
            rank_breakdown=breakdown, event_ids=event_ids, last_seen_at=now, observations=1,
            expires_at=now + timedelta(minutes=self.settings.opportunity_ttl_minutes),
        )
        self.session.add(opportunity)
        self.session.flush()
        return opportunity, True
