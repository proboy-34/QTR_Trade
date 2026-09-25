from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from market_fixtures import EMA_SPEC, candle_payload, oscillating, store
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.auth import Role, StaticTokenAuthProvider
from app.core.config import Settings
from app.core.events import EventBus
from app.global_services.live_paper import LivePaperService
from app.models import Decision, ExecutionPlan, Fill, Order, PortfolioSnapshot, TradeIntent
from app.trading.paper_exchange import PaperExchange


def _plan(session, order_type="MARKET", limit_price=None):
    session.add(PortfolioSnapshot(equity=100_000, available_balance=100_000, exposure=0, margin_used=0, daily_pnl=0, drawdown=0))
    decision = Decision(symbol="BTCUSDT", outcome="TRADE", market_context={}, evaluations=[], confidence=1, reasoning=["r"], correlation_id="c")
    session.add(decision)
    session.flush()
    intent = TradeIntent(decision_id=decision.id, strategy_version_id="v", symbol="BTCUSDT", side="BUY", entry_price=100, confidence=1)
    session.add(intent)
    session.flush()
    plan = ExecutionPlan(trade_intent_id=intent.id, exchange="paper", symbol="BTCUSDT", side="BUY", quantity=Decimal("0.5"),
                         order_type=order_type, risk_amount=1, stop_loss=90, take_profit=110, leverage=1, limit_price=limit_price)
    session.add(plan)
    session.flush()
    return plan, intent


@pytest.mark.asyncio
async def test_market_order_with_off_grid_reference_price_fills_on_tick_grid(session):
    plan, intent = _plan(session)
    result = await PaperExchange(session, Settings(), EventBus()).submit(plan, intent, 100.123456, "tick")
    order = session.get(Order, result["order_id"])
    assert order.status == "FILLED" and result["position_id"]
    fill = session.scalar(select(Fill))
    assert fill.price % Decimal("0.1") == 0 and fill.price > Decimal("100.123456")  # rounded against the buyer


@pytest.mark.asyncio
async def test_limit_order_off_tick_grid_is_still_rejected(session):
    plan, intent = _plan(session, "LIMIT", Decimal("100.05"))
    result = await PaperExchange(session, Settings(), EventBus()).submit(plan, intent, 100, "tick")
    assert result["rejected"] and "INVALID_PRICE_TICK" in result["errors"]


@pytest.mark.asyncio
async def test_live_paper_reports_last_decision_outcome(session):
    frame = oscillating(60)
    store(session, frame.iloc[:-1], "BTCUSDT")
    service = LivePaperService(sessionmaker(bind=session.get_bind(), expire_on_commit=False), Settings(), EventBus())
    assert await service.ingest_closed_candle(candle_payload(frame.iloc[-1], "BTCUSDT"))
    assert service.state.last_decision == "WAIT"


@pytest.mark.asyncio
async def test_backfill_resumed_from_database_handles_naive_sqlite_datetimes(tmp_path):
    from datetime import UTC, datetime

    from sqlalchemy import create_engine, func

    from app.db import Base
    from app.global_services.historical import (
        HistoricalBackfillService,
        SyntheticHistoricalProvider,
    )
    from app.models import MarketCandle, MarketDataBackfill

    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as first:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        job_id = HistoricalBackfillService(first).create("paper", "ETHUSDT", "1h", start, datetime(2026, 1, 3, tzinfo=UTC), 10).id
    with factory() as second:  # values now come back from SQLite without tzinfo
        job = second.get(MarketDataBackfill, job_id)
        assert job.next_start_at.tzinfo is None
        await HistoricalBackfillService(second).run(job, SyntheticHistoricalProvider())
        assert job.status == "COMPLETED" and job.rows_written == 49
        assert second.scalar(select(func.min(MarketCandle.timestamp))) == datetime(2026, 1, 1)
    engine.dispose()


@pytest.mark.asyncio
async def test_token_authentication_and_role_hierarchy():
    provider = StaticTokenAuthProvider("viewer-token:viewer,research-token:researcher,admin-token:admin")
    viewer = await provider.authenticate("Bearer viewer-token")
    assert viewer.can(Role.VIEWER) and not viewer.can(Role.RESEARCHER)
    researcher = await provider.authenticate("Bearer research-token")
    assert researcher.can(Role.RESEARCHER) and not researcher.can(Role.TRADER)
    admin = await provider.authenticate("Bearer admin-token")
    assert admin.can(Role.TRADER) and admin.can(Role.ADMIN)
    with pytest.raises(Exception, match="Invalid token"):
        await provider.authenticate("Bearer nope")
    with pytest.raises(Exception, match="Bearer token required"):
        await provider.authenticate(None)
    with pytest.raises(ValueError):
        Settings(auth_mode="token", auth_tokens="")


@pytest.fixture
def api_app(tmp_path):
    from sqlalchemy import create_engine

    from app.db import Base, get_db
    from app.main import app

    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield app
    app.dependency_overrides.clear()
    engine.dispose()


def test_autonomy_api_surface_and_safety_controls(monkeypatch, api_app):
    from app.core import config

    app = api_app
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()["paths"]
        for path in ("/api/v1/universe", "/api/v1/market/overview", "/api/v1/research/dashboard", "/api/v1/research/hypotheses",
                     "/api/v1/intelligence/events", "/api/v1/ai/status", "/api/v1/memory/knowledge", "/api/v1/learning/dashboard",
                     "/api/v1/portfolio/risk", "/api/v1/safety", "/api/v1/system/providers", "/api/v1/scanner/opportunities",
                     "/api/v1/strategies/overview", "/api/v1/regimes"):
            assert path in schema, path
        assert client.get("/api/v1/universe").json()["run_id"] is None
        overview = client.get("/api/v1/strategies/overview")
        assert overview.status_code == 200 and overview.json()["items"] == []
        dashboard = client.get("/api/v1/research/dashboard").json()
        assert set(dashboard["counts"]) >= {"hypotheses", "experiments", "rejected", "paper_testing", "validated"}
        ai = client.get("/api/v1/ai/status").json()
        assert ai["credential"] == "GEMINI_API_KEY" and "api_key" not in str(ai).lower().replace("gemini_api_key", "")
        created = client.post("/api/v1/research/hypotheses", json={"statement": "EMA crossover test", "spec": EMA_SPEC})
        assert created.status_code == 201 and created.json()["stage"] == "IDEA"
        invalid = client.post("/api/v1/research/hypotheses", json={"statement": "bad spec", "spec": {"entry": [{"left": "eval()", "operator": "gt", "right": 1}]}})
        assert invalid.json()["stage"] == "REJECTED"
        control = client.post("/api/v1/safety/controls", json={"scope": "NEW_ORDERS", "reason": "maintenance window"}).json()
        state = client.get("/api/v1/safety").json()
        assert state["safe_mode"] and state["new_orders_blocked"] and state["real_trading"]["live_execution_adapter_installed"] is False
        assert client.post(f"/api/v1/safety/controls/{control['id']}/clear").json()["active"] is False
        providers = client.get("/api/v1/system/providers").json()
        assert providers["real_trading"] == "DISABLED" and "safety_monitor" in providers["scheduler"]
        assert client.post("/api/v1/intelligence/ingest").status_code == 400  # no provider configured, no fake news
        assert client.get("/api/v1/memory/decisions/missing").status_code == 404

    monkeypatch.setattr(config, "get_settings", lambda: Settings(auth_mode="token", auth_tokens="v:viewer,a:admin"))
    import app.core.auth as auth

    monkeypatch.setattr(auth, "get_settings", lambda: Settings(auth_mode="token", auth_tokens="v:viewer,a:admin"))
    with TestClient(app) as client:
        assert client.post("/api/v1/safety/controls", json={"scope": "SYSTEM", "reason": "no auth"}).status_code == 401
        forbidden = client.post("/api/v1/safety/controls", json={"scope": "SYSTEM", "reason": "viewer"},
                                headers={"Authorization": "Bearer v"})
        assert forbidden.status_code == 403
        allowed = client.post("/api/v1/safety/controls", json={"scope": "SYSTEM", "reason": "admin halt"},
                              headers={"Authorization": "Bearer a"})
        assert allowed.status_code == 201
        client.post(f"/api/v1/safety/controls/{allowed.json()['id']}/clear", headers={"Authorization": "Bearer a"})
