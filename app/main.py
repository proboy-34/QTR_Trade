from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, update

from app.api import event_bus, live_paper_service, router
from app.core.config import get_settings
from app.core.errors import QTRError, qtr_error_handler
from app.core.events import Event
from app.core.logging import configure_logging
from app.core.notifications import TelegramNotifier
from app.core.orchestrator import TaskOrchestrator
from app.core.time import TimeService
from app.db import SessionLocal, engine, init_db
from app.models import JobExecution, Opportunity, SystemEvent
from app.seed import seed_demo

started_at = monotonic()
orchestrator = TaskOrchestrator()


async def scheduler_heartbeat() -> None:
    """The minimal recurring health job; additional jobs register through this boundary."""


async def record_scheduled_job(name: str) -> None:
    with SessionLocal() as session:
        execution = JobExecution(job_name=name, status="RUNNING")
        session.add(execution)
        session.flush()
        session.add(SystemEvent(
            type="ScheduledJobCompleted",
            component="orchestrator",
            severity="INFO",
            message=f"{name} completed",
            payload={"job": name},
            correlation_id=f"scheduler-{name}",
        ))
        if name == "cleanup":
            session.execute(
                update(Opportunity)
                .where(Opportunity.status.in_(["QUEUED", "EVALUATING"]), Opportunity.expires_at < TimeService.now())
                .values(status="EXPIRED")
            )
        execution.status = "COMPLETED"
        execution.finished_at = TimeService.now()
        session.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    init_db()
    if settings.demo_mode:
        with SessionLocal() as session:
            seed_demo(session, settings)
    live_paper_service.recover_interrupted()
    if not orchestrator.states:
        orchestrator.schedule("health_heartbeat", 60, scheduler_heartbeat)
        orchestrator.schedule("market_data_health", 60, lambda: record_scheduled_job("market_data_health"))
        orchestrator.schedule("strategy_refresh", 300, lambda: record_scheduled_job("strategy_refresh"))
        orchestrator.schedule("reconciliation", 300, lambda: record_scheduled_job("reconciliation"))
        orchestrator.schedule("cleanup", 3600, lambda: record_scheduled_job("cleanup"))
    notifier = TelegramNotifier(settings)

    async def notify(event):
        await notifier.send(event.type, event.payload.get("message", event.type))

    categories = {item.strip() for item in settings.notification_categories.split(",")}
    event_categories = {
        "ApplicationStarted": "system", "ApplicationStopped": "system",
        "ExchangeDisconnected": "connection", "ExchangeReconnected": "connection",
        "TradeIntentCreated": "trade", "RiskRejected": "risk",
        "OrderSubmitted": "order", "OrderFilled": "order",
        "PositionOpened": "position", "PositionClosed": "position",
        "HighImpactEventDetected": "intelligence", "SystemError": "system",
        "DailySummary": "summary",
        "LivePaperConnected": "connection", "LivePaperStopped": "system",
    }
    for event_type, category in event_categories.items():
        if category in categories:
            event_bus.subscribe(event_type, notify)
    await event_bus.publish(Event("ApplicationStarted", {"message": "QTR started"}, source="lifecycle"))
    if settings.live_paper_autostart:
        await live_paper_service.start(
            settings.live_paper_provider,
            settings.live_paper_symbol,
            settings.live_paper_timeframe,
        )
    yield
    await live_paper_service.stop()
    await event_bus.publish(Event("ApplicationStopped", {"message": "QTR stopped"}, source="lifecycle"))
    await orchestrator.stop()


app = FastAPI(title="QTR API", version="1.0.0", lifespan=lifespan)
app.add_exception_handler(QTRError, qtr_error_handler)  # type: ignore[arg-type]
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in get_settings().cors_origins.split(",") if item.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(router)


@app.get("/health")
def health() -> dict:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {
        "status": "healthy",
        "mode": get_settings().trading_mode,
        "uptime_seconds": round(monotonic() - started_at, 1),
        "event_bus": {"healthy": event_bus.healthy, "published": event_bus.published, "failures": event_bus.failures},
        "scheduler": {
            name: {
                "enabled": state.enabled, "status": state.status, "runs": state.runs,
                "last_run": state.last_run, "next_run": state.next_run,
                "last_error": state.last_error, "skipped_overlaps": state.skipped_overlaps,
            }
            for name, state in orchestrator.states.items()
        },
        "live_paper": live_paper_service.snapshot(),
    }


@app.get("/health/live")
def live() -> dict: return {"status": "alive"}


@app.get("/health/ready")
def ready() -> dict:
    try:
        with engine.connect() as connection: connection.execute(text("SELECT 1"))
        return {"status": "ready", "database": "healthy"}
    except Exception as exc:
        return {"status": "not_ready", "database": "error", "reason": type(exc).__name__}
