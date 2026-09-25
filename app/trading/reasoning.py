"""Structured, evidence-based trade reasoning (deterministic).

A strategy signal is a *setup*, not a decision. Before a TRADE proposal reaches Risk, the
reasoner gathers evidence that was knowable at decision time and states what supports and
what contradicts the trade. Each timeframe has one role; contradictions are never averaged:

- higher timeframe  -> market structure / context   (may BLOCK a long in a downtrend or panic)
- strategy timeframe -> regime + setup + entry       (declarative rule on closed candles)
- Risk engine        -> size, stop, target (final values)
- exit               -> strategy exit rule, stop, target

Blocking evidence produces NO_TRADE. If contradicting evidence outweighs supporting evidence,
the answer is also NO_TRADE: doing nothing is a valid, successful decision.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.lineage import fingerprint, risk_configuration
from app.core.time import TimeService
from app.global_services.historical import TIMEFRAME_DELTA
from app.global_services.market_intelligence import knowable_at
from app.global_services.regime import load_candles
from app.integrations.fred import macro_as_of
from app.memory.market_memory import MarketMemoryService, context_features
from app.memory.trade_memory import base_asset
from app.models import AssetEligibility, MarketEvent, MarketRegimeRecord, Strategy, StrategyVersion
from app.research.dsl import spec_from_version

HIGHER_TIMEFRAME = {"1m": "15m", "5m": "1h", "15m": "1h", "1h": "4h", "4h": "1d", "1d": None}
SUPPORTIVE_REGIMES = {"TRENDING_UP", "BREAKOUT", "EUPHORIA"}
HOSTILE_REGIMES = {"TRENDING_DOWN", "PANIC"}
BLOCKING_EVENT_TYPES = {"HACK", "EXCHANGE_INCIDENT", "DELISTING"}
TRUSTED = {"SOURCE_VERIFIED", "OPERATOR_ENTERED"}


def _item(code: str, detail: str, source: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "source": source}


class DecisionReasoner:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def _regime(self, exchange: str, symbol: str, timeframe: str, at: Any) -> MarketRegimeRecord | None:
        """Latest regime whose candle had closed by `at` (no look-ahead)."""
        interval = TIMEFRAME_DELTA.get(timeframe, timedelta(hours=1))
        return self.session.scalar(select(MarketRegimeRecord).where(
            MarketRegimeRecord.exchange == exchange, MarketRegimeRecord.symbol == symbol,
            MarketRegimeRecord.timeframe == timeframe, MarketRegimeRecord.candle_timestamp <= at - interval,
        ).order_by(desc(MarketRegimeRecord.candle_timestamp)))

    def assess(self, snapshot: Any, strategy: Strategy, version: StrategyVersion, signal_source: str) -> dict[str, Any]:
        at = TimeService.ensure_utc(snapshot.observed_at)
        supporting: list[dict[str, str]] = []
        contradicting: list[dict[str, str]] = []
        blocking: list[dict[str, str]] = []
        sources: list[str] = [f"{snapshot.exchange}:{snapshot.symbol}:{snapshot.timeframe} candles"]
        spec = spec_from_version(version, strategy)

        supporting.append(_item("SETUP", f"{strategy.name} v{version.version} entry condition true on the closed "
                                         f"{snapshot.timeframe} candle ({signal_source})", "strategy_rule"))
        regime = snapshot.market_regime
        if regime in SUPPORTIVE_REGIMES:
            supporting.append(_item("REGIME", f"{snapshot.timeframe} regime {regime}", "regime_engine"))
        elif regime in HOSTILE_REGIMES:
            contradicting.append(_item("REGIME", f"{snapshot.timeframe} regime {regime} opposes a long", "regime_engine"))
        elif regime in {None, "UNKNOWN"}:
            contradicting.append(_item("REGIME_UNCLEAR", "setup-timeframe regime is UNKNOWN or unavailable", "regime_engine"))

        higher = HIGHER_TIMEFRAME.get(snapshot.timeframe)
        context_regime = self._regime(snapshot.exchange, snapshot.symbol, higher, at) if higher else None
        if context_regime is None:
            contradicting.append(_item("CONTEXT_UNAVAILABLE", f"no closed {higher or 'higher'} timeframe regime available", "regime_engine"))
        elif context_regime.regime in HOSTILE_REGIMES and context_regime.confidence >= 0.6:
            blocking.append(_item("HIGHER_TIMEFRAME", f"{higher} structure {context_regime.regime} "
                                                      f"(confidence {context_regime.confidence:.2f}) — no longs against it", "regime_engine"))
        elif context_regime.regime in SUPPORTIVE_REGIMES:
            supporting.append(_item("HIGHER_TIMEFRAME", f"{higher} structure {context_regime.regime}", "regime_engine"))

        if snapshot.symbol != "BTCUSDT":
            btc = self._regime(snapshot.exchange, "BTCUSDT", snapshot.timeframe, at)
            if btc and btc.regime == "PANIC":
                blocking.append(_item("BTC_PANIC", "BTC is in a PANIC regime; correlated altcoin longs are blocked", "regime_engine"))
            elif btc and btc.regime == "TRENDING_DOWN":
                contradicting.append(_item("BTC_CONTEXT", "BTC is trending down", "regime_engine"))
            elif btc and btc.regime in SUPPORTIVE_REGIMES:
                supporting.append(_item("BTC_CONTEXT", f"BTC regime {btc.regime}", "regime_engine"))

        eligibility = self.session.scalar(select(AssetEligibility).where(
            AssetEligibility.exchange == snapshot.exchange, AssetEligibility.symbol == snapshot.symbol,
        ).order_by(desc(AssetEligibility.evaluated_at)))
        if eligibility and eligibility.eligible:
            metrics = eligibility.metrics or {}
            supporting.append(_item("LIQUIDITY", f"eligible; 24h quote volume {metrics.get('quote_volume_24h')}, "
                                                 f"spread {metrics.get('spread_bps')} bps", "binance_universe"))
            sources.append(f"universe run {eligibility.run_id}")
        elif snapshot.liquidity == "STRONG" and snapshot.exchange == "paper":
            supporting.append(_item("LIQUIDITY", "demo snapshot declares strong liquidity", "operator_snapshot"))
        else:
            contradicting.append(_item("LIQUIDITY", "no current eligibility/liquidity evidence", "binance_universe"))

        base = base_asset(snapshot.symbol)
        known = knowable_at()
        events = [event for event in self.session.scalars(select(MarketEvent).where(
            known >= at - timedelta(hours=24), known <= at).limit(300)).all()
            if base in (event.affected_assets or []) or "RISK_ASSETS" in (event.affected_assets or [])]
        for event in events:
            trusted = event.verification_status in TRUSTED
            label = f"{event.event_type}: {event.title} ({event.verification_status}, {event.source})"
            if trusted and event.event_type in BLOCKING_EVENT_TYPES:
                blocking.append(_item("EVENT_RISK", label, f"event:{event.id}"))
            elif trusted and event.severity in {"HIGH", "CRITICAL"}:
                contradicting.append(_item("EVENT_RISK", label, f"event:{event.id}"))
        upcoming = [event for event in self.session.scalars(select(MarketEvent).where(
            MarketEvent.category == "MACRO", MarketEvent.event_at > at, MarketEvent.event_at <= at + timedelta(hours=2),
            MarketEvent.severity.in_(["HIGH", "CRITICAL"]), MarketEvent.retrieved_at <= at,
        )).all()]
        for event in upcoming:
            contradicting.append(_item("SCHEDULED_MACRO", f"{event.title} scheduled at {event.event_at}", f"event:{event.id}"))

        macro = macro_as_of(self.session, at)
        historical: dict[str, Any] = {"status": "insufficient"}
        frame = load_candles(self.session, snapshot.exchange, snapshot.symbol, snapshot.timeframe, 300)
        features = context_features(frame) if not frame.empty else {}
        if features:
            analogues = MarketMemoryService(self.session).similar(
                features, timeframe=snapshot.timeframe, limit=25, exclude_after=at - TIMEFRAME_DELTA.get(snapshot.timeframe, timedelta(hours=1)) * 12)
            summary = analogues.get("summary", {})
            historical = {"status": "ok", **summary, "note": analogues.get("note")}
            if (summary.get("with_known_outcome") or 0) >= 10:
                median = summary.get("median_forward_return_pct") or 0
                target = supporting if median > 0 else contradicting
                target.append(_item("HISTORICAL_ANALOGUES", f"{summary['with_known_outcome']} similar situations; median "
                                                            f"forward return {median}% over {summary.get('horizon_bars')} bars", "market_memory"))

        risk = spec.risk if spec else None
        horizon = (f"{risk.max_holding_bars} × {snapshot.timeframe}" if risk and risk.max_holding_bars
                   else f"until the strategy exit rule, stop or target ({snapshot.timeframe} bars)")
        invalidation = ["price trades through the risk-engine stop"]
        if spec and spec.exit:
            invalidation.append("strategy exit rule: " + "; ".join(f"{c.left} {c.operator} {c.right}" for c in spec.exit))
        strength = len(supporting) / max(1, len(supporting) + len(contradicting))
        if blocking:
            verdict, reason = "NO_TRADE", "blocking evidence: " + "; ".join(item["detail"] for item in blocking)
        elif len(contradicting) > len(supporting):
            verdict, reason = "NO_TRADE", "contradicting evidence outweighs supporting evidence"
        else:
            verdict, reason = "TRADE_PROPOSAL", "setup confirmed with more supporting than contradicting evidence"
        return {
            "decision": verdict, "reason": reason, "asset": snapshot.symbol, "direction": "LONG",
            "decision_time": at.isoformat(),
            "timeframe_roles": {"context": higher, "regime": snapshot.timeframe, "setup": snapshot.timeframe,
                                "entry": snapshot.timeframe, "risk": "risk_engine", "exit": "strategy exit rule + stop/target"},
            "strategy": {"id": strategy.id, "name": strategy.name, "version": version.version,
                         "version_id": version.id, "content_hash": version.content_hash, "status": strategy.status},
            "thesis": strategy.description or f"{strategy.name} setup on {snapshot.symbol}",
            "market_regime": {"setup": regime, "context": context_regime.regime if context_regime else None},
            "supporting_evidence": supporting, "contradicting_evidence": contradicting, "blocking_evidence": blocking,
            "evidence_strength": round(strength, 3), "invalidation": invalidation, "time_horizon": horizon,
            "news_context": [event.id for event in events], "macro_context": macro, "historical_context": historical,
            "data_sources": sources + [f"event:{event.id}" for event in events] + [item["provenance"] for item in macro.values()],
            "risk_config_version": fingerprint(risk_configuration(self.settings))[:16],
            "plan": None, "ai_review": {"status": "NOT_REQUESTED"},
        }
