"""Scheduled autonomous work. Every job is idempotent, restart-safe and persisted as a JobExecution."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import Session

from app.ai.providers import build_provider
from app.ai.research import AIResearchService
from app.core.config import Settings
from app.core.events import EventBus
from app.core.logging import redact
from app.core.time import TimeService
from app.global_services.historical import (
    HISTORICAL_PROVIDERS,
    TIMEFRAME_DELTA,
    TIMEFRAME_MS,
    BinanceHistoricalProvider,
    HistoricalBackfillService,
)
from app.global_services.market_intelligence import IntelligenceService, configured_providers
from app.global_services.scanner import MarketScanner
from app.global_services.universe import BinanceUniverseProvider, UniverseService
from app.integrations.fred import MacroService
from app.integrations.health import ProviderVerifier
from app.learning.counterfactual import CounterfactualService
from app.learning.post_trade import PostTradeAnalyst
from app.learning.strategy_health import StrategyHealthService
from app.memory.trade_memory import TradeMemoryService
from app.models import (
    Hypothesis,
    HypothesisStage,
    JobExecution,
    MarketCandle,
    Opportunity,
    Position,
    PostTradeAnalysis,
    ProviderCheck,
    SystemEvent,
    TradeMemory,
)
from app.research.generator import ResearchGenerator
from app.research.hypotheses import TERMINAL, HypothesisEngine
from app.trading.safety import SafetyMonitor, SafetyService

logger = logging.getLogger("qtr.jobs")
SessionFactory = Callable[[], Session]


@dataclass
class JobContext:
    session_factory: SessionFactory
    settings: Settings
    event_bus: EventBus
    live_state: Callable[[], dict[str, Any]] | None = None


async def run_job(context: JobContext, name: str, job: Callable[[Session], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Persist the run, its outcome and details; failures are recorded and re-raised to the scheduler."""
    with context.session_factory() as session:
        execution = JobExecution(job_name=name, status="RUNNING")
        session.add(execution)
        session.commit()
        execution_id = execution.id
    with context.session_factory() as session:
        try:
            details = await job(session)
            status, error = "COMPLETED", None
        except Exception as exc:
            session.rollback()
            details, status, error = {}, "FAILED", redact(f"{type(exc).__name__}: {exc}", context.settings.secret_values())[:2000]
    with context.session_factory() as session:
        record = session.get(JobExecution, execution_id)
        if record:
            record.status, record.error, record.details = status, error, _jsonable(details)
            record.finished_at = TimeService.now()
            record.duration_ms = max(0, round((record.finished_at - TimeService.ensure_utc(record.started_at)).total_seconds() * 1000))
        session.add(SystemEvent(
            type="ScheduledJobCompleted" if status == "COMPLETED" else "ScheduledJobFailed",
            component="orchestrator", severity="INFO" if status == "COMPLETED" else "WARNING",
            message=f"{name} {status.lower()}", payload={"job": name, "error": error},
            correlation_id=f"scheduler-{name}",
        ))
        session.commit()
    if error:
        raise RuntimeError(error)
    return details


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def scan_symbols(session: Session, settings: Settings, exchange: str, timeframe: str) -> list[str]:
    symbols = UniverseService.eligible_symbols(session, exchange)
    if symbols:
        return symbols
    # Without a universe run (e.g. offline), scan whatever has enough stored history.
    rows = session.execute(select(MarketCandle.symbol, func.count()).where(
        MarketCandle.exchange == exchange, MarketCandle.timeframe == timeframe,
    ).group_by(MarketCandle.symbol)).all()
    return [symbol for symbol, count in rows if count >= 60][: settings.universe_max_assets]


def system_halted(session: Session) -> bool:
    return any(control.scope == "SYSTEM" for control in SafetyService(session).active())


def jobs(context: JobContext) -> dict[str, tuple[int, Callable[[Session], Awaitable[dict[str, Any]]]]]:
    settings, bus = context.settings, context.event_bus

    async def universe_refresh(session: Session) -> dict[str, Any]:
        provider = BinanceUniverseProvider(settings.binance_public_base_url)
        timeframe = settings.csv("scanner_timeframes")[0]
        return await UniverseService(session, settings, provider).refresh(timeframe)

    async def market_data_sync(session: Session) -> dict[str, Any]:
        if system_halted(session):
            return {"skipped": "SYSTEM_STOP"}
        exchange = settings.scanner_exchange
        provider = (BinanceHistoricalProvider(settings.binance_public_base_url) if exchange == "binance"
                    else HISTORICAL_PROVIDERS.get(exchange))
        if provider is None:
            return {"skipped": f"no historical provider for {exchange}"}
        synced: dict[str, int] = {}
        for timeframe in settings.csv("scanner_timeframes"):
            interval_ms = TIMEFRAME_MS[timeframe]
            now_ms = int(TimeService.now().timestamp() * 1000)
            last_closed = datetime.fromtimestamp(((now_ms // interval_ms) * interval_ms - interval_ms) / 1000, UTC)
            for symbol in UniverseService.eligible_symbols(session, exchange):
                latest = session.scalar(select(MarketCandle.timestamp).where(
                    MarketCandle.exchange == exchange, MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe,
                ).order_by(desc(MarketCandle.timestamp)))
                start = (TimeService.ensure_utc(latest) + TIMEFRAME_DELTA[timeframe]) if latest else (
                    last_closed - TIMEFRAME_DELTA[timeframe] * (settings.universe_min_history_candles + 100))
                if start > last_closed:
                    continue
                service = HistoricalBackfillService(session)
                job = service.create(exchange, symbol, timeframe, start, last_closed, 1000)
                await service.run(job, provider)
                synced[f"{symbol}:{timeframe}"] = job.rows_written
        return {"synced": synced}

    async def market_scan(session: Session) -> dict[str, Any]:
        if system_halted(session):
            return {"skipped": "SYSTEM_STOP"}
        results = {}
        for timeframe in settings.csv("scanner_timeframes"):
            symbols = scan_symbols(session, settings, settings.scanner_exchange, timeframe)
            outcome = await MarketScanner(session, settings, bus).scan(settings.scanner_exchange, timeframe, symbols)
            results[timeframe] = {"scanned": outcome["scanned"], "opportunities": outcome["opportunities"]}
        return results

    async def news_ingestion(session: Session) -> dict[str, Any]:
        providers = configured_providers(settings, session)
        if not providers:
            return {"skipped": "FINNHUB_API_KEY not configured"}
        results: dict[str, Any] = {}
        for provider in providers:
            try:
                results[provider.name] = await IntelligenceService(session, bus).ingest(provider)
            except (ConnectionError, ValueError) as exc:
                session.rollback()
                results[provider.name] = {"error": redact(str(exc), settings.secret_values())[:300]}
        if all("error" in item for item in results.values()):
            raise ConnectionError("all news sources failed: " + "; ".join(item["error"] for item in results.values()))
        return results

    async def macro_ingestion(session: Session) -> dict[str, Any]:
        report = await MacroService(session, settings).ingest()
        if report and not report.get("skipped") and all("error" in item for item in report.values()):
            raise ConnectionError("all FRED series failed: " + next(iter(report.values()))["error"])
        return report

    async def provider_verification(session: Session) -> dict[str, Any]:
        # Real generation calls cost tokens: prove Gemini generation at most every 6 hours.
        last = session.scalar(select(ProviderCheck.checked_at).where(
            ProviderCheck.provider == "gemini", ProviderCheck.check == "structured_generation").order_by(desc(ProviderCheck.checked_at)))
        generate = last is None or TimeService.now() - TimeService.ensure_utc(last) > timedelta(hours=6)
        result = await ProviderVerifier(settings).run(session, generate=generate)
        return {name: item["state"] for name, item in result["providers"].items()}

    async def testnet_reconciliation(session: Session) -> dict[str, Any]:
        from app.execution.binance_testnet import BinanceTestnetExchange

        exchange = BinanceTestnetExchange(session, settings, bus)
        report = await exchange.reconcile()
        await exchange.sync_portfolio()
        session.commit()
        return report

    async def research_queue(session: Session) -> dict[str, Any]:
        if system_halted(session) or not settings.autonomous_research_enabled:
            return {"skipped": "SYSTEM_STOP" if system_halted(session) else "AUTONOMOUS_RESEARCH_ENABLED=false"}
        generated = ResearchGenerator(session, settings, bus).from_opportunities(limit=2)
        ai_result: dict[str, Any] | None = None
        provider = build_provider(settings)
        if provider.configured:
            top = session.scalar(select(Opportunity).where(
                Opportunity.source == "scanner", Opportunity.status == "DETECTED", Opportunity.rank_score >= 60,
            ).order_by(desc(Opportunity.rank_score)))
            if top:
                ai_result = await AIResearchService(session, settings, bus, provider).generate_hypotheses(
                    top.exchange or settings.scanner_exchange, top.symbol, top.timeframe or "1h")
                top.status = "QUEUED"
                session.commit()
        engine = HypothesisEngine(session, settings, bus)
        advanced = []
        pending = [item for item in session.scalars(
            select(Hypothesis).where(Hypothesis.stage.notin_(list(TERMINAL))).order_by(Hypothesis.updated_at).limit(200)
        ).all() if item.spec][: settings.research_max_hypotheses_per_run]
        for hypothesis in pending:
            advanced.append(await engine.advance(hypothesis))
        return {"generated": generated, "ai": ai_result, "advanced": advanced}

    async def learning(session: Session) -> dict[str, Any]:
        memory = TradeMemoryService(session)
        recorded = 0
        closed = session.scalars(select(Position).where(
            Position.status == "CLOSED", Position.id.notin_(select(TradeMemory.position_id)),
        ).limit(200)).all()
        for position in closed:
            if memory.record(position):
                recorded += 1
        analyst = PostTradeAnalyst(session)
        unanalyzed = session.scalars(select(TradeMemory).where(
            TradeMemory.id.notin_(select(PostTradeAnalysis.trade_memory_id))
        ).limit(200)).all()
        for trade in unanalyzed:
            analyst.analyze(trade)
        session.commit()
        counterfactuals = CounterfactualService(session, settings.counterfactual_horizon_bars)
        captured = counterfactuals.capture()
        evaluated = counterfactuals.evaluate_pending()
        session.commit()
        health = await StrategyHealthService(session, settings, bus).evaluate_all()
        ai_trade = None
        provider = build_provider(settings)
        if provider.configured and unanalyzed:
            ai_trade = await AIResearchService(session, settings, bus, provider).analyze_trade(unanalyzed[-1].id)
        return {"trade_memory_recorded": recorded, "post_trade_analyses": len(unanalyzed),
                "counterfactuals_captured": captured, "counterfactuals": evaluated,
                "strategy_health": [{key: item[key] for key in ("strategy_id", "status")} for item in health],
                "ai_trade_analysis": ai_trade}

    async def opportunity_cleanup(session: Session) -> dict[str, Any]:
        result = session.execute(
            update(Opportunity)
            .where(Opportunity.status.in_(["DETECTED", "QUEUED", "EVALUATING"]), Opportunity.expires_at < TimeService.now())
            .values(status="EXPIRED")
        )
        session.commit()
        return {"expired": getattr(result, "rowcount", 0)}

    async def data_integrity(session: Session) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for timeframe in settings.csv("scanner_timeframes"):
            interval = TIMEFRAME_DELTA[timeframe]
            since = TimeService.now() - timedelta(days=1)
            for symbol in scan_symbols(session, settings, settings.scanner_exchange, timeframe):
                stamps = [TimeService.ensure_utc(item) for item in session.scalars(select(MarketCandle.timestamp).where(
                    MarketCandle.exchange == settings.scanner_exchange, MarketCandle.symbol == symbol,
                    MarketCandle.timeframe == timeframe, MarketCandle.timestamp >= since,
                ).order_by(MarketCandle.timestamp)).all()]
                gaps = sum(max(0, round((b - a) / interval) - 1) for a, b in zip(stamps, stamps[1:], strict=False))
                stale = not stamps or TimeService.now() - stamps[-1] > interval * 3
                if gaps or stale:
                    report[f"{symbol}:{timeframe}"] = {"missing_candles": gaps, "stale": stale}
        return {"issues": report, "checked_at": TimeService.now().isoformat()}

    async def safety_monitor(session: Session) -> dict[str, Any]:
        state = context.live_state() if context.live_state else None
        return await SafetyMonitor(session, settings, bus).check(state)

    registered: dict[str, tuple[int, Callable[[Session], Awaitable[dict[str, Any]]]]] = {
        "safety_monitor": (settings.safety_monitor_interval_seconds, safety_monitor),
        "opportunity_cleanup": (600, opportunity_cleanup),
        "learning": (settings.learning_interval_seconds, learning),
        "research_queue": (settings.research_interval_seconds, research_queue),
        "data_integrity": (3600, data_integrity),
        "news_ingestion": (settings.news_ingestion_interval_seconds, news_ingestion),
        "macro_ingestion": (settings.macro_ingestion_interval_seconds, macro_ingestion),
        "provider_verification": (settings.provider_check_interval_seconds, provider_verification),
    }
    if settings.execution_mode == "testnet":
        registered["testnet_reconciliation"] = (300, testnet_reconciliation)
    if settings.market_scanner_enabled:
        registered["universe_refresh"] = (settings.universe_refresh_seconds, universe_refresh)
        registered["market_scan"] = (settings.scanner_interval_seconds, market_scan)
    if settings.market_sync_enabled:
        registered["market_data_sync"] = (settings.market_sync_interval_seconds, market_data_sync)
    return registered


def hypothesis_backlog(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Hypothesis).where(
        Hypothesis.stage.notin_([HypothesisStage.REJECTED, HypothesisStage.FAILED, HypothesisStage.ARCHIVED])
    )) or 0


JOB_PURPOSE = {
    "safety_monitor": "Automatic safe mode on loss/drawdown, stale data, provider failure, repeated rejections, corrupt state",
    "opportunity_cleanup": "Expire scanner/decision opportunities past their time-to-live",
    "learning": "Trade memory, post-trade analysis, counterfactuals, strategy health",
    "research_queue": "Generate hypotheses from evidence and advance them through validation gates",
    "data_integrity": "Detect missing or stale candles for scanned assets",
    "news_ingestion": "Fetch Finnhub crypto news and (if the plan allows) the economic calendar",
    "macro_ingestion": "Fetch FRED series with point-in-time vintages",
    "provider_verification": "Real connectivity/capability checks for Binance, Gemini, Finnhub, FRED",
    "universe_refresh": "Rebuild the eligible Binance universe from exchange metadata",
    "market_scan": "Scan eligible assets for unusual conditions (never trades)",
    "market_data_sync": "Backfill closed candles for eligible assets from Binance",
    "testnet_reconciliation": "Refresh non-final testnet orders and testnet balances",
}
RETRY_POLICY = "No immediate retry: a failed run is recorded as FAILED and retried at the next scheduled run; provider calls retry transient errors internally."
