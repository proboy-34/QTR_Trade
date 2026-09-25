"""API for the autonomous research platform: universe, scanner, intelligence, AI, research,
memory, learning, portfolio intelligence and safety. All figures come from persisted state."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.ai.providers import build_provider
from app.ai.research import AIResearchService
from app.api import event_bus, live_paper_service, page
from app.core.auth import Principal, Role, require
from app.core.config import get_settings
from app.core.errors import NotFoundError, QTRError
from app.core.serialization import serialize
from app.core.time import TimeService
from app.db import SessionLocal, get_db
from app.global_services.historical import TIMEFRAME_DELTA
from app.global_services.market_intelligence import (
    IntelligenceService,
    configured_providers,
    provider_status,
)
from app.global_services.regime import load_candles
from app.global_services.scanner import MarketScanner
from app.global_services.universe import UniverseService
from app.jobs import JobContext, jobs, run_job, scan_symbols
from app.learning.paper_validation import trade_statistics
from app.learning.post_trade import learning_summary
from app.memory.knowledge import KnowledgeBase
from app.memory.market_memory import MarketMemoryService, context_features
from app.memory.trade_memory import reconstruct_decision
from app.models import (
    AIArtifact,
    AICallRecord,
    AssetEligibility,
    CounterfactualEvaluation,
    DiscrepancyReport,
    Experiment,
    Hypothesis,
    KnowledgeEntry,
    MarketDataValidationFailure,
    MarketEvent,
    MarketRegimeRecord,
    Opportunity,
    PostTradeAnalysis,
    SafetyControl,
    Strategy,
    StrategyHealthRecord,
    TradeMemory,
    ValidationResult,
)
from app.research.challengers import ChallengerService
from app.research.generator import ResearchGenerator
from app.research.hypotheses import HypothesisEngine, research_counts
from app.schemas_autonomy import (
    AITaskRequest,
    ChallengerCreate,
    HypothesisDecision,
    ResearchHypothesisCreate,
    SafetyControlCreate,
    ScanRequest,
    SimilarityRequest,
)
from app.trading.portfolio_intelligence import PortfolioIntelligence
from app.trading.safety import SafetyService

router = APIRouter(prefix="/api/v1", tags=["autonomy"])
researcher = require(Role.RESEARCHER)
trader = require(Role.TRADER)
admin = require(Role.ADMIN)


def job_context() -> JobContext:
    return JobContext(SessionLocal, get_settings(), event_bus, live_paper_service.snapshot)


async def run_named_job(name: str) -> dict[str, Any]:
    context = job_context()
    registered = jobs(context)
    if name not in registered:
        raise QTRError(f"Job {name} is disabled by configuration")
    try:
        return await run_job(context, name, registered[name][1])
    except RuntimeError as exc:
        raise QTRError(f"{name} failed: {exc}") from exc


# ----------------------------------------------------------------- universe/market
@router.get("/universe")
def universe(exchange: str = "binance", eligible_only: bool = False, session: Session = Depends(get_db)) -> dict:
    run_id = UniverseService.latest_run(session, exchange)
    if not run_id:
        return {"run_id": None, "items": [], "eligible": 0, "evaluated": 0,
                "note": "No universe refresh has completed yet (requires public Binance access)."}
    query = select(AssetEligibility).where(AssetEligibility.run_id == run_id)
    if eligible_only:
        query = query.where(AssetEligibility.eligible.is_(True))
    rows = session.scalars(query.order_by(desc(AssetEligibility.eligible), AssetEligibility.rank, AssetEligibility.symbol).limit(500)).all()
    evaluated = session.scalar(select(func.count()).select_from(AssetEligibility).where(AssetEligibility.run_id == run_id)) or 0
    eligible = session.scalar(select(func.count()).select_from(AssetEligibility).where(
        AssetEligibility.run_id == run_id, AssetEligibility.eligible.is_(True))) or 0
    return {"run_id": run_id, "evaluated_at": rows[0].evaluated_at if rows else None, "evaluated": evaluated,
            "eligible": eligible, "items": [serialize(row) for row in rows]}


@router.post("/universe/refresh")
async def refresh_universe(_: Principal = Depends(researcher)) -> dict:
    return await run_named_job("universe_refresh")


@router.get("/market/overview")
def market_overview(exchange: str | None = None, timeframe: str = "1h", session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    exchange = exchange or settings.scanner_exchange
    symbols = scan_symbols(session, settings, exchange, timeframe)
    if not symbols and exchange != "paper":
        exchange, symbols = "paper", scan_symbols(session, settings, "paper", timeframe)
    bars_per_day = max(1, int(timedelta(days=1) / TIMEFRAME_DELTA.get(timeframe, timedelta(hours=1))))
    items = []
    for symbol in symbols[:100]:
        frame = load_candles(session, exchange, symbol, timeframe, 300)
        if frame.empty:
            continue
        close = frame["close"]
        latest_at = TimeService.ensure_utc(frame["timestamp"].iloc[-1].to_pydatetime())
        features = context_features(frame)
        regime = session.scalar(select(MarketRegimeRecord).where(
            MarketRegimeRecord.exchange == exchange, MarketRegimeRecord.symbol == symbol,
            MarketRegimeRecord.timeframe == timeframe,
        ).order_by(desc(MarketRegimeRecord.candle_timestamp)))
        failures = session.scalar(select(func.count()).select_from(MarketDataValidationFailure).where(
            MarketDataValidationFailure.exchange == exchange, MarketDataValidationFailure.symbol == symbol,
            MarketDataValidationFailure.created_at >= TimeService.now() - timedelta(days=1),
        )) or 0
        stale = TimeService.now() - latest_at > TIMEFRAME_DELTA.get(timeframe, timedelta(hours=1)) * 3
        items.append({
            "symbol": symbol, "exchange": exchange, "price": float(close.iloc[-1]),
            "change_24h_pct": round((float(close.iloc[-1]) / float(close.iloc[-bars_per_day - 1]) - 1) * 100, 3)
            if len(close) > bars_per_day else None,
            "volume_24h": round(float(frame["volume"].iloc[-bars_per_day:].sum()), 4),
            "volatility": features.get("volatility"), "rsi": features.get("rsi"),
            "regime": regime.regime if regime else None, "regime_confidence": regime.confidence if regime else None,
            "data_quality": "STALE" if stale else ("DEGRADED" if failures else "GOOD"),
            "validation_failures_24h": failures, "last_candle_at": latest_at.isoformat(),
            "is_demo": exchange == "paper",
        })
    return {"exchange": exchange, "timeframe": timeframe, "items": items}


@router.post("/scanner/run")
async def run_scanner(payload: ScanRequest, _: Principal = Depends(researcher), session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    exchange = payload.exchange or settings.scanner_exchange
    symbols = [item.upper() for item in payload.symbols] or scan_symbols(session, settings, exchange, payload.timeframe)
    if SafetyService(session).active() and any(c.scope == "SYSTEM" for c in SafetyService(session).active()):
        raise QTRError("SYSTEM stop is active; scanning is halted")
    return await MarketScanner(session, settings, event_bus).scan(exchange, payload.timeframe, symbols)


@router.get("/scanner/opportunities")
def scanner_opportunities(status: str | None = None, symbol: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria: list[Any] = [Opportunity.source == "scanner"]
    if status:
        criteria.append(Opportunity.status == status)
    if symbol:
        criteria.append(Opportunity.symbol == symbol.upper())
    rows = session.scalars(select(Opportunity).where(*criteria).order_by(
        desc(Opportunity.last_seen_at), desc(Opportunity.rank_score)).limit(200)).all()
    events = {event.id: event for event in session.scalars(select(MarketEvent).where(
        MarketEvent.id.in_({item for row in rows for item in (row.event_ids or [])}))).all()}
    return {"items": [{**serialize(row), "events": [
        {"id": event_id, "title": events[event_id].title, "verification_status": events[event_id].verification_status}
        for event_id in (row.event_ids or []) if event_id in events]} for row in rows],
        "note": "Rank is an evidence-based attention priority, not a forecast of return."}


@router.get("/regimes")
def regimes(timeframe: str | None = None, session: Session = Depends(get_db)) -> dict:
    latest = select(MarketRegimeRecord.exchange, MarketRegimeRecord.symbol, MarketRegimeRecord.timeframe,
                    func.max(MarketRegimeRecord.candle_timestamp).label("at")).group_by(
        MarketRegimeRecord.exchange, MarketRegimeRecord.symbol, MarketRegimeRecord.timeframe).subquery()
    query = select(MarketRegimeRecord).join(latest, (MarketRegimeRecord.exchange == latest.c.exchange)
                                            & (MarketRegimeRecord.symbol == latest.c.symbol)
                                            & (MarketRegimeRecord.timeframe == latest.c.timeframe)
                                            & (MarketRegimeRecord.candle_timestamp == latest.c.at))
    if timeframe:
        query = query.where(MarketRegimeRecord.timeframe == timeframe)
    return {"items": [serialize(row) for row in session.scalars(query.order_by(MarketRegimeRecord.symbol)).all()]}


@router.get("/regimes/history")
def regime_history(symbol: str, timeframe: str = "1h", transitions_only: bool = False,
                   session: Session = Depends(get_db)) -> dict:
    query = select(MarketRegimeRecord).where(MarketRegimeRecord.symbol == symbol.upper(), MarketRegimeRecord.timeframe == timeframe)
    if transitions_only:
        query = query.where(MarketRegimeRecord.is_transition.is_(True))
    return {"items": [serialize(row) for row in session.scalars(query.order_by(desc(MarketRegimeRecord.candle_timestamp)).limit(500)).all()]}


# -------------------------------------------------------------------- intelligence
@router.get("/intelligence/events")
def intelligence_events(verification: str | None = None, event_type: str | None = None, asset: str | None = None,
                        category: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria: list[Any] = []
    if verification:
        criteria.append(MarketEvent.verification_status == verification)
    if event_type:
        criteria.append(MarketEvent.event_type == event_type)
    if category:
        criteria.append(MarketEvent.category == category)
    rows = session.scalars(select(MarketEvent).where(*criteria).order_by(desc(MarketEvent.event_at)).limit(300)).all()
    if asset:
        rows = [row for row in rows if asset.upper() in (row.affected_assets or [])]
    return {"items": [serialize(row) for row in rows]}


@router.get("/intelligence/providers")
def intelligence_providers() -> dict:
    return provider_status(get_settings())


@router.post("/intelligence/ingest")
async def ingest_intelligence(_: Principal = Depends(researcher), session: Session = Depends(get_db)) -> dict:
    providers = configured_providers(get_settings())
    if not providers:
        raise QTRError("No news or macro provider configured. Set NEWS_PROVIDER/NEWS_PROVIDER_API_KEY or MACRO_PROVIDER/MACRO_PROVIDER_API_KEY.")
    return {provider.name: await IntelligenceService(session, event_bus).ingest(provider) for provider in providers}


# ------------------------------------------------------------------------------ AI
@router.get("/ai/status")
def ai_status(session: Session = Depends(get_db)) -> dict:
    return AIResearchService(session, get_settings(), event_bus).gateway.status()


@router.get("/ai/calls")
def ai_calls(session: Session = Depends(get_db)) -> dict:
    return page(session, AICallRecord, 1, 200)


@router.get("/ai/artifacts")
def ai_artifacts(task_type: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria = (AIArtifact.task_type == task_type,) if task_type else ()
    return page(session, AIArtifact, 1, 100, *criteria)


@router.post("/ai/tasks")
async def ai_task(payload: AITaskRequest, _: Principal = Depends(researcher), session: Session = Depends(get_db)) -> dict:
    service = AIResearchService(session, get_settings(), event_bus)
    if payload.task == "market_context":
        return await service.market_context(payload.exchange, payload.symbol, payload.timeframe)
    if payload.task == "hypotheses":
        return await service.generate_hypotheses(payload.exchange, payload.symbol, payload.timeframe)
    if not payload.subject_id:
        raise QTRError("subject_id is required for this task")
    if payload.task == "interpret_event":
        return await service.interpret_event(payload.subject_id)
    if payload.task == "analyze_trade":
        return await service.analyze_trade(payload.subject_id)
    return await service.explain_degradation(payload.subject_id)


# ------------------------------------------------------------------------ research
@router.get("/research/dashboard")
def research_dashboard(session: Session = Depends(get_db)) -> dict:
    counts = research_counts(session)
    recent = session.scalars(select(Hypothesis).order_by(desc(Hypothesis.updated_at)).limit(20)).all()
    running = session.scalars(select(Experiment).where(Experiment.status == "RUNNING").limit(20)).all()
    candidates = session.scalars(select(Strategy).where(Strategy.status.in_(["paper_testing", "ready_for_review"]))).all()
    return {"counts": counts, "recent_hypotheses": [serialize(item) for item in recent],
            "running_experiments": [serialize(item) for item in running],
            "candidates": [serialize(item) for item in candidates]}


@router.get("/research/hypotheses")
def research_hypotheses(stage: str | None = None, origin: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria: list[Any] = []
    if stage:
        criteria.append(Hypothesis.stage == stage)
    if origin:
        criteria.append(Hypothesis.origin == origin)
    return page(session, Hypothesis, 1, 200, *criteria)


@router.post("/research/hypotheses", status_code=201)
def create_research_hypothesis(payload: ResearchHypothesisCreate, _: Principal = Depends(researcher),
                               session: Session = Depends(get_db)) -> dict:
    hypothesis = HypothesisEngine(session, get_settings(), event_bus).create(
        payload.statement, origin="operator", spec=payload.spec, description=payload.description,
        market_conditions=payload.market_conditions, assumptions=payload.assumptions,
        variables={"exchange": payload.exchange} if payload.exchange else {},
    )
    return serialize(hypothesis)


@router.get("/research/hypotheses/{hypothesis_id}")
def research_hypothesis(hypothesis_id: str, session: Session = Depends(get_db)) -> dict:
    hypothesis = session.get(Hypothesis, hypothesis_id)
    if not hypothesis:
        raise NotFoundError("Hypothesis not found")
    experiments = session.scalars(select(Experiment).where(
        Experiment.id.in_((hypothesis.evidence or {}).get("experiment_ids", [])))).all()
    return {**serialize(hypothesis), "experiments": [
        {key: value for key, value in serialize(item).items() if key != "result"} | {
            "metrics": (item.result or {}).get("metrics") or (item.result or {}).get("aggregate")}
        for item in experiments]}


@router.post("/research/hypotheses/{hypothesis_id}/advance")
async def advance_hypothesis(hypothesis_id: str, run_to_paper: bool = False, _: Principal = Depends(researcher),
                             session: Session = Depends(get_db)) -> dict:
    hypothesis = session.get(Hypothesis, hypothesis_id)
    if not hypothesis:
        raise NotFoundError("Hypothesis not found")
    engine = HypothesisEngine(session, get_settings(), event_bus)
    return await (engine.run_until_paper(hypothesis) if run_to_paper else engine.advance(hypothesis))


@router.post("/research/hypotheses/{hypothesis_id}/reject")
def reject_hypothesis(hypothesis_id: str, payload: HypothesisDecision, principal: Principal = Depends(researcher),
                      session: Session = Depends(get_db)) -> dict:
    hypothesis = session.get(Hypothesis, hypothesis_id)
    if not hypothesis:
        raise NotFoundError("Hypothesis not found")
    engine = HypothesisEngine(session, get_settings(), event_bus)
    engine._reject(hypothesis, "REJECTED", "OPERATOR_REJECTED", {"reason": payload.reason, "by": principal.subject})
    session.commit()
    return serialize(hypothesis)


@router.post("/research/generate")
def generate_research(_: Principal = Depends(researcher), session: Session = Depends(get_db)) -> dict:
    return {"created": ResearchGenerator(session, get_settings(), event_bus).from_opportunities(limit=5)}


@router.post("/research/run")
async def run_research_queue(_: Principal = Depends(researcher)) -> dict:
    return await run_named_job("research_queue")


# ---------------------------------------------------------------------- strategies
@router.get("/strategies/overview")
def strategies_overview(session: Session = Depends(get_db)) -> dict:
    items = []
    for strategy in session.scalars(select(Strategy).order_by(desc(Strategy.updated_at))).all():
        version = max(strategy.versions, key=lambda item: item.version) if strategy.versions else None
        version_ids = [item.id for item in strategy.versions]
        trades = session.scalars(select(TradeMemory).where(TradeMemory.strategy_version_id.in_(version_ids))
                                 .order_by(TradeMemory.closed_at)).all() if version_ids else []
        validations = session.scalars(select(ValidationResult).where(
            ValidationResult.strategy_version_id == version.id)).all() if version else []
        regime_performance = {}
        for regime in {trade.regime_at_entry or "UNKNOWN" for trade in trades}:
            subset = [trade for trade in trades if (trade.regime_at_entry or "UNKNOWN") == regime]
            regime_performance[regime] = {**trade_statistics(subset), "evidence": "sufficient" if len(subset) >= 5 else "insufficient"}
        items.append({
            **serialize(strategy), "version": version.version if version else None,
            "version_id": version.id if version else None,
            "parameters": version.parameters if version else {},
            "validations": [{"method": item.method, "result": item.result, "created_at": item.created_at} for item in validations],
            "paper_performance": trade_statistics(list(trades)),
            "recent_performance": trade_statistics(list(trades[-20:])),
            "regime_performance": {key: {k: v for k, v in value.items() if k in {"trades", "expectancy_pct", "profit_factor", "win_rate", "evidence"}}
                                   for key, value in regime_performance.items()},
        })
    return {"items": items}


@router.post("/strategies/{strategy_id}/challengers", status_code=201)
def create_challenger(strategy_id: str, payload: ChallengerCreate, _: Principal = Depends(researcher),
                      session: Session = Depends(get_db)) -> dict:
    return serialize(ChallengerService(session, get_settings(), event_bus).propose(
        strategy_id, parameters=payload.parameters, spec_overrides=payload.spec_overrides, rationale=payload.rationale))


@router.get("/strategies/{strategy_id}/health")
def strategy_health(strategy_id: str, session: Session = Depends(get_db)) -> dict:
    return page(session, StrategyHealthRecord, 1, 100, StrategyHealthRecord.strategy_id == strategy_id)


# -------------------------------------------------------------------------- memory
@router.get("/memory/decisions/{decision_id}")
def decision_memory(decision_id: str, session: Session = Depends(get_db)) -> dict:
    result = reconstruct_decision(session, decision_id)
    if result is None:
        raise NotFoundError("Decision not found")
    return result


@router.post("/memory/market/similar")
def similar_market(payload: SimilarityRequest, session: Session = Depends(get_db)) -> dict:
    frame = load_candles(session, payload.exchange, payload.symbol, payload.timeframe, 300)
    if frame.empty:
        raise NotFoundError("No market data for this symbol/timeframe/exchange")
    features = context_features(frame)
    now = TimeService.ensure_utc(frame["timestamp"].iloc[-1].to_pydatetime())
    result = MarketMemoryService(session).similar(
        features, timeframe=payload.timeframe, symbol=payload.symbol if payload.same_symbol_only else None,
        limit=payload.limit, horizon_bars=payload.horizon_bars, exclude_after=now,
    )
    return {"current": {"symbol": payload.symbol, "features": features, "as_of": now.isoformat()}, **result}


@router.get("/memory/trades")
def trade_memory(symbol: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria = (TradeMemory.symbol == symbol.upper(),) if symbol else ()
    return page(session, TradeMemory, 1, 200, *criteria)


@router.get("/memory/knowledge")
def knowledge(kind: str | None = None, symbol: str | None = None, regime: str | None = None,
              q: str | None = Query(None, max_length=200), source_type: str | None = None,
              session: Session = Depends(get_db)) -> dict:
    base = KnowledgeBase(session)
    return {"counts": base.counts(), "items": [serialize(item) for item in base.query(
        kind=kind, symbol=symbol, regime=regime, text=q, source_type=source_type)]}


# ------------------------------------------------------------------------ learning
@router.get("/learning/dashboard")
def learning_dashboard(session: Session = Depends(get_db)) -> dict:
    counterfactuals = dict(session.execute(select(CounterfactualEvaluation.verdict, func.count())
                                           .group_by(CounterfactualEvaluation.verdict)).all())
    experiments = dict(session.execute(select(Experiment.status, func.count()).group_by(Experiment.status)).all())
    health = dict(session.execute(select(Strategy.health_status, func.count()).group_by(Strategy.health_status)).all())
    knowledge_counts = KnowledgeBase(session).counts()
    degraded = session.scalars(select(Strategy).where(Strategy.health_status.in_(["DEGRADED", "REVIEW_REQUIRED"]))).all()
    lessons = session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.kind.in_(["TRADE_LESSON", "STRATEGY_BEHAVIOR", "MARKET_PATTERN", "COUNTERFACTUAL_FINDING"]))
                              .order_by(desc(KnowledgeEntry.evidence_count), desc(KnowledgeEntry.created_at)).limit(20)).all()
    return {
        "observed": {"trades_recorded": session.scalar(select(func.count()).select_from(TradeMemory)) or 0,
                     "counterfactuals": sum(value for key, value in counterfactuals.items() if key) ,
                     "counterfactuals_pending": session.scalar(select(func.count()).select_from(CounterfactualEvaluation)
                                                               .where(CounterfactualEvaluation.status == "PENDING")) or 0},
        "tested": {"experiments": experiments, "successful": experiments.get("COMPLETED", 0), "failed": experiments.get("FAILED", 0)},
        "learned": {"knowledge": knowledge_counts, **learning_summary(session)},
        "counterfactual_verdicts": {key or "PENDING": value for key, value in counterfactuals.items()},
        "strategy_health": health,
        "degraded_strategies": [{"id": item.id, "name": item.name, "health_status": item.health_status,
                                 "reasons": (item.health_details or {}).get("reasons", [])} for item in degraded],
        "top_patterns": [serialize(item) for item in lessons],
    }


@router.get("/learning/post-trade")
def post_trade(session: Session = Depends(get_db)) -> dict:
    return page(session, PostTradeAnalysis, 1, 200)


@router.get("/learning/counterfactuals")
def counterfactuals(verdict: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria = (CounterfactualEvaluation.verdict == verdict,) if verdict else ()
    return page(session, CounterfactualEvaluation, 1, 200, *criteria)


@router.get("/learning/discrepancies")
def discrepancies(session: Session = Depends(get_db)) -> dict:
    return page(session, DiscrepancyReport, 1, 100)


@router.post("/learning/run")
async def run_learning(_: Principal = Depends(researcher)) -> dict:
    return await run_named_job("learning")


# ------------------------------------------------------------------ portfolio/safety
@router.get("/portfolio/risk")
def portfolio_risk(session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    return {**PortfolioIntelligence(session).analyze(), "limits": {
        "max_total_exposure": settings.max_total_exposure, "max_correlated_exposure": settings.max_correlated_exposure,
        "max_symbol_concentration": settings.max_symbol_concentration, "correlation_threshold": settings.correlation_threshold,
        "max_daily_loss": settings.max_daily_loss, "max_drawdown": settings.max_drawdown}}


@router.get("/safety")
def safety(session: Session = Depends(get_db)) -> dict:
    active = SafetyService(session).active()
    history = session.scalars(select(SafetyControl).order_by(desc(SafetyControl.triggered_at)).limit(100)).all()
    settings = get_settings()
    return {"safe_mode": bool(active), "new_orders_blocked": bool(SafetyService(session).blocks_new_orders()),
            "active": [serialize(item) for item in active], "history": [serialize(item) for item in history],
            "real_trading": {"trading_mode": settings.trading_mode, "live_trading_enabled": settings.live_trading_enabled,
                             "live_execution_adapter_installed": False}}


@router.post("/safety/controls", status_code=201)
async def activate_safety(payload: SafetyControlCreate, principal: Principal = Depends(trader),
                          session: Session = Depends(get_db)) -> dict:
    try:
        control, created = SafetyService(session).activate(
            payload.scope, payload.reason, target=payload.target.upper() if payload.scope == "ASSET" else payload.target,
            trigger="OPERATOR", source=principal.subject)
    except ValueError as exc:
        raise QTRError(str(exc)) from exc
    session.commit()
    return {**serialize(control), "created": created}


@router.post("/safety/controls/{control_id}/clear")
def clear_safety(control_id: str, principal: Principal = Depends(admin), session: Session = Depends(get_db)) -> dict:
    control = SafetyService(session).clear(control_id, principal.subject)
    if not control:
        raise NotFoundError("Safety control not found")
    session.commit()
    return serialize(control)


@router.get("/system/providers")
def system_providers(session: Session = Depends(get_db)) -> dict:
    from app.main import orchestrator

    settings = get_settings()
    live = live_paper_service.snapshot()
    ai = build_provider(settings)
    universe_run = session.scalar(select(AssetEligibility).order_by(desc(AssetEligibility.evaluated_at)))
    return {
        "binance": {"market_data": "public (no credentials)", "live_stream": live["status"], "connected": live["connected"],
                    "last_universe_refresh": universe_run.evaluated_at if universe_run else None,
                    "private_trading": "disabled (no authenticated adapter installed)",
                    "credentials_configured": bool(settings.binance_api_key and settings.binance_api_secret)},
        "news": provider_status(settings),
        "ai": {"provider": ai.name, "model": ai.model, "configured": ai.configured, "enabled": settings.ai_enabled},
        "database": {"status": "healthy", "dialect": session.get_bind().dialect.name},
        "scheduler": {name: {"status": state.status, "runs": state.runs, "last_run": state.last_run,
                             "next_run": state.next_run, "last_error": state.last_error}
                      for name, state in orchestrator.states.items()},
        "event_bus": {"published": event_bus.published, "failures": event_bus.failures, "healthy": event_bus.healthy},
        "safety": {"active_controls": len(SafetyService(session).active())},
        "real_trading": "DISABLED",
    }
