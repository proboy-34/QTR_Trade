from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query, WebSocket
from sqlalchemy import desc, func, select
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import Session

from app.core.auth import Principal, local_principal
from app.core.config import get_settings
from app.core.errors import NotFoundError, QTRError
from app.core.events import Event, EventBus, publish_persisted
from app.db import SessionLocal, get_db
from app.global_services.assets import AssetRegistryService
from app.global_services.backtest import BacktestEngine
from app.global_services.connections import ConnectionManager
from app.global_services.exchanges import EXCHANGES
from app.global_services.features import enrich
from app.global_services.historical import HISTORICAL_PROVIDERS, HistoricalBackfillService
from app.global_services.live_paper import LivePaperService
from app.global_services.market_intelligence import is_high_impact
from app.global_services.strategy_repository import StrategyRepository
from app.global_services.validation import WalkForwardConfig, WalkForwardValidator
from app.models import (
    Asset,
    AssetInstrument,
    Dataset,
    Decision,
    ExecutionReport,
    Experiment,
    Fill,
    Hypothesis,
    IntegrationMetadata,
    JobExecution,
    LivePaperSession,
    MarketCandle,
    MarketContextRecord,
    MarketDataBackfill,
    MarketDataValidationFailure,
    MarketEvent,
    Observation,
    Opportunity,
    Order,
    PortfolioSnapshot,
    Position,
    PositionEvent,
    ReconciliationIssue,
    ResearchPlan,
    RiskEvent,
    Strategy,
    StrategyVersion,
    SystemEvent,
    TradeIntent,
    ValidationResult,
)
from app.research.analysis import ResearchAnalyzer
from app.research.experiments import ExperimentManager
from app.research.review import ResearchReviewService
from app.schemas import (
    BackfillCreate,
    BacktestRequest,
    ExperimentAction,
    ExperimentCreate,
    HypothesisCreate,
    LivePaperStart,
    MarketEventCreate,
    MarketSnapshotInput,
    ObservationCreate,
    PositionAction,
    ReconciliationRequest,
    ResearchPlanCreate,
    StrategyCreate,
    StrategyVersionCreate,
    TransitionRequest,
    WalkForwardRequest,
)
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.positions import PositionManager
from app.trading.reconciliation import ExchangeState, ReconciliationService
from app.trading.recovery import RecoveryService

router = APIRouter(prefix="/api/v1")
event_bus = EventBus()
connection_manager = ConnectionManager(event_bus)
live_paper_service = LivePaperService(SessionLocal, get_settings(), event_bus)


def serialize(obj: Any) -> dict[str, Any]:
    return {column.key: getattr(obj, column.key) for column in inspect(obj).mapper.column_attrs}


def candle_frame(rows: Sequence[Any]) -> pd.DataFrame:
    frame = pd.DataFrame([serialize(row) for row in rows])
    for column in ("open", "high", "low", "close", "volume", "funding_rate", "open_interest"):
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def page(session: Session, model: Any, page_number: int, page_size: int, *criteria: Any) -> dict:
    query = select(model)
    count_query = select(func.count()).select_from(model)
    if criteria:
        query, count_query = query.where(*criteria), count_query.where(*criteria)
    if hasattr(model, "created_at"):
        query = query.order_by(desc(model.created_at))
    items = session.scalars(query.offset((page_number - 1) * page_size).limit(page_size)).all()
    return {"items": [serialize(item) for item in items], "total": session.scalar(count_query) or 0, "page": page_number, "page_size": page_size}


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    portfolio = session.scalar(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.captured_at)))
    latest_candle = session.scalar(select(MarketCandle).order_by(desc(MarketCandle.timestamp)))
    latest_decision = session.scalar(select(Decision).order_by(desc(Decision.created_at)))
    active_strategies = session.scalar(select(func.count()).select_from(Strategy).where(Strategy.status == "active")) or 0
    return {
        "system": {"status": "healthy", "trading_mode": settings.trading_mode, "database": "healthy", "event_bus": "healthy" if event_bus.healthy else "warning", "scheduler": "healthy", "demo_mode": settings.demo_mode},
        "portfolio": serialize(portfolio) if portfolio else {},
        "market": serialize(latest_candle) if latest_candle else {},
        "counts": {
            "open_positions": session.scalar(select(func.count()).select_from(Position).where(Position.status == "OPEN")) or 0,
            "active_orders": session.scalar(select(func.count()).select_from(Order).where(Order.status.in_(["NEW", "PARTIALLY_FILLED"]))) or 0,
            "active_strategies": active_strategies,
            "events": session.scalar(select(func.count()).select_from(SystemEvent)) or 0,
        },
        "latest_decision": serialize(latest_decision) if latest_decision else None,
        "integrations": [
            serialize(item) for item in session.scalars(select(IntegrationMetadata)).all()
        ],
    }


@router.get("/research/plans")
def list_plans(page_number: int = Query(1, alias="page", ge=1), page_size: int = Query(20, le=100), session: Session = Depends(get_db)) -> dict:
    return page(session, ResearchPlan, page_number, page_size)


@router.post("/research/plans", status_code=201)
def create_plan(payload: ResearchPlanCreate, session: Session = Depends(get_db)) -> dict:
    plan = ResearchPlan(**payload.model_dump())
    session.add(plan); session.commit(); session.refresh(plan)
    return serialize(plan)


@router.get("/experiments")
def list_experiments(session: Session = Depends(get_db)) -> dict:
    return page(session, Experiment, 1, 100)


@router.post("/experiments", status_code=201)
def create_experiment(payload: ExperimentCreate, session: Session = Depends(get_db)) -> dict:
    item = ExperimentManager(session).create(
        payload.name, payload.configuration, payload.research_plan_id
    )
    return serialize(item)


@router.post("/experiments/{experiment_id}/actions")
def experiment_action(
    experiment_id: str, payload: ExperimentAction, session: Session = Depends(get_db)
) -> dict:
    experiment = session.get(Experiment, experiment_id)
    if not experiment:
        raise NotFoundError("Experiment not found")
    manager = ExperimentManager(session)
    action = payload.action.upper()
    if action == "RUN":
        def runner(configuration: dict) -> dict:
            symbol = configuration.get("symbol", "BTCUSDT")
            timeframe = configuration.get("timeframe", "1h")
            rows = session.scalars(
                select(MarketCandle).where(
                    MarketCandle.symbol == symbol, MarketCandle.timeframe == timeframe
                ).order_by(MarketCandle.timestamp)
            ).all()
            frame = candle_frame(rows)
            return BacktestEngine().run(
                frame,
                fast=int(configuration.get("fast", 10)),
                slow=int(configuration.get("slow", 30)),
            )

        manager.execute(experiment, runner)
    else:
        target = {"PAUSE": "PAUSED", "RESUME": "RUNNING", "CANCEL": "CANCELLED", "QUEUE": "QUEUED"}.get(action)
        if not target:
            raise QTRError("Unsupported experiment action")
        manager.transition(experiment, target)
    return serialize(experiment)


@router.get("/observations")
def observations(session: Session = Depends(get_db)) -> dict:
    return page(session, Observation, 1, 100)


@router.post("/observations", status_code=201)
def create_observation(payload: ObservationCreate, session: Session = Depends(get_db)) -> dict:
    item = Observation(**payload.model_dump())
    session.add(item)
    session.commit()
    session.refresh(item)
    return serialize(item)


@router.get("/hypotheses")
def hypotheses(session: Session = Depends(get_db)) -> dict:
    return page(session, Hypothesis, 1, 100)


@router.post("/hypotheses", status_code=201)
def create_hypothesis(payload: HypothesisCreate, session: Session = Depends(get_db)) -> dict:
    item = Hypothesis(**payload.model_dump())
    session.add(item)
    session.commit()
    session.refresh(item)
    return serialize(item)


@router.get("/datasets")
def list_datasets(session: Session = Depends(get_db)) -> dict:
    return page(session, Dataset, 1, 100)


@router.get("/strategies")
def list_strategies(status: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria = (Strategy.status == status,) if status else ()
    result = page(session, Strategy, 1, 100, *criteria)
    for item in result["items"]:
        versions = session.scalars(select(StrategyVersion).where(StrategyVersion.strategy_id == item["id"]).order_by(desc(StrategyVersion.version))).all()
        item["latest_version"] = serialize(versions[0]) if versions else None
    return result


@router.post("/strategies", status_code=201)
def create_strategy(payload: StrategyCreate, session: Session = Depends(get_db)) -> dict:
    data = payload.model_dump()
    return serialize(StrategyRepository(session).create(data))


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str, session: Session = Depends(get_db)) -> dict:
    strategy = session.get(Strategy, strategy_id)
    if not strategy: raise NotFoundError("Strategy not found")
    result = serialize(strategy)
    result["versions"] = [serialize(item) for item in sorted(strategy.versions, key=lambda v: v.version, reverse=True)]
    version_ids = [item["id"] for item in result["versions"]]
    validations = session.scalars(select(ValidationResult).where(ValidationResult.strategy_version_id.in_(version_ids))).all() if version_ids else []
    result["validations"] = [serialize(item) for item in validations]
    return result


@router.post("/strategies/{strategy_id}/versions", status_code=201)
def add_version(strategy_id: str, payload: StrategyVersionCreate, session: Session = Depends(get_db)) -> dict:
    return serialize(StrategyRepository(session).add_version(strategy_id, payload.model_dump()))


@router.post("/strategies/{strategy_id}/transition")
def transition_strategy(strategy_id: str, payload: TransitionRequest, session: Session = Depends(get_db)) -> dict:
    return serialize(StrategyRepository(session).transition(strategy_id, payload.status, payload.reason))


@router.post("/backtests")
def run_backtest(payload: BacktestRequest, session: Session = Depends(get_db)) -> dict:
    candles = session.scalars(select(MarketCandle).where(MarketCandle.symbol == payload.symbol, MarketCandle.timeframe == payload.timeframe).order_by(MarketCandle.timestamp)).all()
    frame = candle_frame(candles)
    if frame.empty: raise NotFoundError("No market data found for this symbol and timeframe")
    return BacktestEngine().run(frame, **payload.model_dump(exclude={"strategy_version_id", "symbol", "timeframe"}))


@router.post("/validation/walk-forward", status_code=201)
def walk_forward(payload: WalkForwardRequest, session: Session = Depends(get_db)) -> dict:
    rows = session.scalars(
        select(MarketCandle).where(
            MarketCandle.symbol == payload.symbol,
            MarketCandle.timeframe == payload.timeframe,
        ).order_by(MarketCandle.timestamp)
    ).all()
    if not rows:
        raise NotFoundError("No market data available for validation")
    frame = candle_frame(rows)
    config = WalkForwardConfig(
        train_size=payload.train_size,
        validation_size=payload.validation_size,
        step_size=payload.step_size,
    )
    validator = WalkForwardValidator()
    result = validator.run(frame, config, fast=payload.fast, slow=payload.slow)
    record = validator.persist(
        session, payload.strategy_version_id, payload.dataset_id, result, config
    )
    return serialize(record)


@router.get("/validation")
def validations(session: Session = Depends(get_db)) -> dict:
    return page(session, ValidationResult, 1, 100)


@router.get("/research/knowledge")
def knowledge_base(session: Session = Depends(get_db)) -> dict:
    models = (ResearchPlan, Experiment, Observation, Hypothesis, Strategy, ValidationResult)
    return {
        "counts": {
            model.__tablename__: session.scalar(select(func.count()).select_from(model)) or 0
            for model in models
        },
        "failed_experiments": [
            serialize(item)
            for item in session.scalars(
                select(Experiment).where(Experiment.status == "FAILED").order_by(desc(Experiment.updated_at)).limit(20)
            ).all()
        ],
    }


@router.post("/research/analyze")
def analyze_research(symbol: str = "BTCUSDT", session: Session = Depends(get_db)) -> dict:
    rows = session.scalars(
        select(MarketCandle).where(MarketCandle.symbol == symbol).order_by(MarketCandle.timestamp)
    ).all()
    if not rows:
        raise NotFoundError("No research data")
    return ResearchAnalyzer().analyze(candle_frame(rows))


@router.post("/research/review/{strategy_id}")
def review_research(strategy_id: str, session: Session = Depends(get_db)) -> dict:
    strategy = session.get(Strategy, strategy_id)
    if not strategy:
        raise NotFoundError("Strategy not found")
    version = max(strategy.versions, key=lambda item: item.version)
    result = ResearchReviewService().review({
        "symbol": strategy.symbol,
        "timeframe": strategy.timeframe,
        "entry_rules": version.entry_rules,
        "exit_rules": version.exit_rules,
        "parameters": version.parameters,
        "documentation": version.documentation,
    })
    return {"status": result.status, "errors": result.errors, "warnings": result.warnings}


@router.get("/market/candles")
def candles(
    symbol: str = "BTCUSDT", exchange: str | None = None, timeframe: str | None = None,
    limit: int = Query(120, ge=1, le=1000), session: Session = Depends(get_db),
) -> list[dict]:
    criteria = [MarketCandle.symbol == symbol]
    if exchange:
        criteria.append(MarketCandle.exchange == exchange)
    if timeframe:
        criteria.append(MarketCandle.timeframe == timeframe)
    rows = session.scalars(select(MarketCandle).where(*criteria).order_by(desc(MarketCandle.timestamp)).limit(limit)).all()
    return [serialize(row) for row in reversed(rows)]


@router.post("/market-data/backfills", status_code=201)
def create_backfill(payload: BackfillCreate, session: Session = Depends(get_db)) -> dict:
    if payload.exchange not in HISTORICAL_PROVIDERS:
        raise QTRError("Historical provider is not supported")
    job = HistoricalBackfillService(session).create(
        payload.exchange, payload.symbol, payload.timeframe,
        payload.start_at, payload.end_at, payload.batch_limit,
    )
    return serialize(job)


@router.get("/market-data/backfills")
def backfills(session: Session = Depends(get_db)) -> dict:
    return page(session, MarketDataBackfill, 1, 100)


@router.get("/market-data/backfills/{backfill_id}")
def backfill(backfill_id: str, session: Session = Depends(get_db)) -> dict:
    job = session.get(MarketDataBackfill, backfill_id)
    if not job:
        raise NotFoundError("Backfill not found")
    return serialize(job)


@router.post("/market-data/backfills/{backfill_id}/resume")
async def resume_backfill(backfill_id: str, session: Session = Depends(get_db)) -> dict:
    job = session.get(MarketDataBackfill, backfill_id)
    if not job:
        raise NotFoundError("Backfill not found")
    provider = HISTORICAL_PROVIDERS.get(job.exchange)
    if not provider:
        raise QTRError("Historical provider is not supported")
    await HistoricalBackfillService(session).run(job, provider)
    return serialize(job)


@router.get("/market-data/validation-failures")
def validation_failures(session: Session = Depends(get_db)) -> dict:
    return page(session, MarketDataValidationFailure, 1, 200)


@router.get("/market/context")
def market_context(
    symbol: str = "BTCUSDT", exchange: str = "paper", timeframe: str = "1h",
    session: Session = Depends(get_db),
) -> dict:
    rows = session.scalars(select(MarketCandle).where(
        MarketCandle.symbol == symbol,
        MarketCandle.exchange == exchange,
        MarketCandle.timeframe == timeframe,
    ).order_by(desc(MarketCandle.timestamp)).limit(100)).all()
    if not rows: raise NotFoundError("No market data")
    frame = enrich(candle_frame(list(reversed(rows))))
    latest = frame.iloc[-1]
    return {"symbol": symbol, "exchange": exchange, "timeframe": timeframe, "price": latest.close, "regime": latest.regime.upper(), "direction": latest.trend.upper(), "volatility": latest.volatility, "rsi": latest.rsi, "funding": latest.get("funding_rate", 0), "liquidity": "STRONG"}


@router.get("/market/live/{exchange}")
async def live_ticker(exchange: str, symbol: str = "BTCUSDT") -> dict:
    adapter = EXCHANGES.get(exchange)
    if not adapter:
        raise NotFoundError("Exchange adapter not found")
    return await adapter.ticker(symbol)


@router.get("/market/events")
def market_events(session: Session = Depends(get_db)) -> dict:
    return page(session, MarketEvent, 1, 100)


@router.post("/market/events", status_code=201)
async def create_market_event(payload: MarketEventCreate, session: Session = Depends(get_db)) -> dict:
    item = MarketEvent(**payload.model_dump())
    session.add(item)
    session.flush()
    if is_high_impact(item.category, item.severity):
        await publish_persisted(
            session,
            event_bus,
            Event("HighImpactEventDetected", {"market_event_id": item.id}),
            "market_intelligence",
        )
    session.commit()
    session.refresh(item)
    return serialize(item)


@router.get("/assets")
def assets(session: Session = Depends(get_db)) -> dict:
    return page(session, Asset, 1, 100)


@router.get("/assets/instruments")
def asset_instruments(
    exchange: str | None = None, session: Session = Depends(get_db)
) -> dict:
    criteria = (AssetInstrument.exchange == exchange,) if exchange else ()
    return page(session, AssetInstrument, 1, 200, *criteria)


@router.get("/assets/{symbol}/mappings")
def asset_mappings(symbol: str, session: Session = Depends(get_db)) -> dict:
    return {"symbol": symbol, "mappings": AssetRegistryService(session).cross_exchange_map(symbol)}


@router.get("/market/contexts")
def market_context_history(session: Session = Depends(get_db)) -> dict:
    return page(session, MarketContextRecord, 1, 100)


@router.get("/opportunities")
def opportunities(status: str | None = None, session: Session = Depends(get_db)) -> dict:
    criteria = (Opportunity.status == status,) if status else ()
    return page(session, Opportunity, 1, 100, *criteria)


@router.post("/decision/evaluate")
async def evaluate(payload: MarketSnapshotInput, session: Session = Depends(get_db)) -> dict:
    data = payload.model_dump(exclude={"force_signal"})
    return await TradingPipeline(session, get_settings(), event_bus).evaluate(MarketSnapshot(**data), payload.force_signal)


@router.get("/decisions")
def decisions(session: Session = Depends(get_db)) -> dict: return page(session, Decision, 1, 100)


@router.get("/trade-intents")
def intents(session: Session = Depends(get_db)) -> dict: return page(session, TradeIntent, 1, 100)


@router.get("/positions")
def positions(session: Session = Depends(get_db)) -> dict: return page(session, Position, 1, 100)


@router.post("/positions/{position_id}/actions")
def position_action(
    position_id: str, payload: PositionAction, session: Session = Depends(get_db)
) -> dict:
    position = session.get(Position, position_id)
    if not position:
        raise NotFoundError("Position not found")
    manager = PositionManager(session)
    action = payload.action.upper()
    if action == "MARK" and payload.price:
        manager.mark(position, payload.price)
    elif action == "BREAK_EVEN":
        manager.move_to_break_even(position)
    elif action == "PARTIAL_CLOSE" and payload.price and payload.quantity:
        manager.partial_close(position, payload.quantity, payload.price, payload.reason)
    elif action == "CLOSE" and payload.price:
        manager.close(position, payload.price, payload.reason)
    else:
        raise QTRError("Invalid position action or missing price")
    return serialize(position)


@router.get("/position-events")
def position_events(session: Session = Depends(get_db)) -> dict:
    return page(session, PositionEvent, 1, 200)


@router.get("/risk-events")
def risk_events(session: Session = Depends(get_db)) -> dict:
    return page(session, RiskEvent, 1, 200)


@router.get("/orders")
def orders(status: str | None = None, session: Session = Depends(get_db)) -> dict:
    return page(session, Order, 1, 100, *((Order.status == status,) if status else ()))


@router.get("/fills")
def fills(session: Session = Depends(get_db)) -> dict: return page(session, Fill, 1, 100)


@router.get("/execution-reports")
def execution_reports(session: Session = Depends(get_db)) -> dict:
    return page(session, ExecutionReport, 1, 100)


@router.post("/reconciliation")
def reconcile(payload: ReconciliationRequest, session: Session = Depends(get_db)) -> dict:
    issues = ReconciliationService().reconcile(
        session,
        payload.exchange,
        ExchangeState(orders=payload.orders, positions=payload.positions),
    )
    return {"issues": [serialize(item) for item in issues], "corrective_orders": 0}


@router.get("/reconciliation/issues")
def reconciliation_issues(session: Session = Depends(get_db)) -> dict:
    return page(session, ReconciliationIssue, 1, 200)


@router.get("/system/events")
def events(session: Session = Depends(get_db)) -> dict: return page(session, SystemEvent, 1, 200)


@router.get("/system/jobs")
def job_executions(session: Session = Depends(get_db)) -> dict:
    return page(session, JobExecution, 1, 200)


@router.get("/connections")
def connections() -> dict:
    return connection_manager.snapshot()


@router.post("/connections/{provider}/test")
async def test_public_connection(provider: str) -> dict:
    adapter = EXCHANGES.get(provider)
    if not adapter:
        raise NotFoundError("Exchange adapter not found")
    state = await connection_manager.connect(provider, adapter)
    return state.__dict__


@router.post("/recovery")
def recover(payload: ReconciliationRequest, session: Session = Depends(get_db)) -> dict:
    return RecoveryService().recover(
        session,
        payload.exchange,
        ExchangeState(orders=payload.orders, positions=payload.positions),
    )


@router.get("/auth/me")
def auth_me(principal: Principal = Depends(local_principal)) -> dict:
    return {
        "subject": principal.subject,
        "roles": sorted(role.value for role in principal.roles),
        "provider": principal.provider,
        "local_demo": principal.provider == "local_demo",
    }


@router.get("/integrations")
def integrations(session: Session = Depends(get_db)) -> list[dict]:
    return [serialize(item) for item in session.scalars(select(IntegrationMetadata)).all()]


@router.post("/integrations/{provider}/test")
def test_integration(provider: str, session: Session = Depends(get_db)) -> dict:
    item = session.scalar(select(IntegrationMetadata).where(IntegrationMetadata.provider == provider))
    if not item: raise NotFoundError("Integration not found")
    item.last_checked_at = datetime.now(UTC)
    item.connection_status = "configured" if item.configured else "not_configured"
    item.last_error = None if item.configured else "Credentials are not configured in the server environment"
    session.commit()
    return serialize(item)


@router.get("/live-paper/status")
def live_paper_status() -> dict:
    return live_paper_service.snapshot()


@router.post("/live-paper/start", status_code=202)
async def start_live_paper(payload: LivePaperStart) -> dict:
    return await live_paper_service.start(
        payload.provider, payload.symbol.upper(), payload.timeframe
    )


@router.post("/live-paper/stop")
async def stop_live_paper() -> dict:
    return await live_paper_service.stop()


@router.get("/live-paper/sessions")
def live_paper_sessions(session: Session = Depends(get_db)) -> dict:
    return page(session, LivePaperSession, 1, 100)


@router.get("/settings")
def public_settings() -> dict:
    settings = get_settings()
    return {
        "app_env": settings.app_env,
        "trading_mode": settings.trading_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "demo_mode": settings.demo_mode,
        "live_paper": {
            "provider": settings.live_paper_provider,
            "symbol": settings.live_paper_symbol,
            "timeframe": settings.live_paper_timeframe,
            "autostart": settings.live_paper_autostart,
            "paper_only": True,
        },
        "risk": {
            "max_risk_per_trade": settings.max_risk_per_trade,
            "max_total_exposure": settings.max_total_exposure,
            "max_daily_loss": settings.max_daily_loss,
            "max_drawdown": settings.max_drawdown,
            "max_open_positions": settings.max_open_positions,
            "max_leverage": settings.max_leverage,
        },
    }


@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    await websocket.accept()
    while True:
        await websocket.send_json({"type": "heartbeat", "timestamp": datetime.now(UTC).isoformat(), "event_bus": {"published": event_bus.published, "failures": event_bus.failures}, "live_paper": live_paper_service.snapshot()})
        import asyncio
        await asyncio.sleep(2)
