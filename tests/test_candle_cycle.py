"""DETERMINISTIC FIXTURE TESTS of the REST closed-candle paper loop.

Candles, quotes and AI answers here are fixtures. Nothing in this file is a real-market trade;
real-provider verification is run separately against Binance/Gemini/Finnhub/FRED.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import numpy as np
import pandas as pd
import pytest
from market_fixtures import FakeUniverseProvider, instrument, liquid
from paper_fixtures import FeedQuotes, validated_version
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.ai.providers import AIRequest, AIResponse
from app.core.config import Settings
from app.core.events import EventBus
from app.db import Base
from app.global_services.quotes import BinanceQuoteProvider, QuoteUnavailable
from app.global_services.universe import UniverseService
from app.integrations.fred import MacroService, parse_vintages
from app.models import (
    Decision,
    MarketCandle,
    MarketEvent,
    MarketRegimeRecord,
    Order,
    PortfolioSnapshot,
    Position,
    PostTradeAnalysis,
    ProcessedCandle,
    TradeMemory,
)
from app.research.dsl import parse_spec, version_payload
from app.trading.candle_cycle import ClosedCandleCycle, candle_exit, last_closed_open
from app.trading.paper_account import PaperAccount
from app.trading.safety import SafetyService

HOUR = timedelta(hours=1)
SPEC = {"entry": [{"left": "close", "operator": "gt", "right": 0}], "exit": [{"left": "close", "operator": "lt", "right": 0}],
        "timeframes": ["1h"], "universe": ["SOLUSDT"],
        "risk": {"stop_loss_pct": 0.03, "take_profit_pct": 0.06, "max_holding_bars": 4}}


def frame(end_open: datetime, n: int = 400, start: float = 100.0) -> pd.DataFrame:
    """Gently rising fixture market (no randomness)."""
    closes = start * np.exp(np.linspace(0, 0.08, n)) * (1 + 0.004 * np.sin(np.arange(n) / 3))
    data = pd.DataFrame({"timestamp": pd.date_range(end=end_open, periods=n, freq="h", tz="UTC"), "close": closes})
    data["open"] = np.r_[closes[0], closes[:-1]]
    data["high"] = np.maximum(data["open"], data["close"]) * 1.002
    data["low"] = np.minimum(data["open"], data["close"]) * 0.998
    data["volume"] = 1000.0
    return data


class FrameCandles:
    """Fixture candle provider serving closed candles from dataframes."""

    name = "binance"

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames, self.calls = frames, 0

    async def fetch_batch(self, symbol, timeframe, start, end, limit):
        self.calls += 1
        data = self.frames[symbol]
        rows = data[(data["timestamp"] >= pd.Timestamp(start)) & (data["timestamp"] <= pd.Timestamp(end))].head(limit)
        return [{"timestamp": row.timestamp.to_pydatetime(), "open": str(round(row.open, 6)), "high": str(round(row.high, 6)),
                 "low": str(round(row.low, 6)), "close": str(round(row.close, 6)), "volume": str(row.volume)}
                for row in rows.itertuples()]


class FixtureAI:
    name, model = "fixture-ai", "fixture"

    def __init__(self, decision: dict | None = None, error: Exception | None = None) -> None:
        self.decision, self.error, self.calls = decision, error, 0

    @property
    def configured(self) -> bool:
        return True

    async def generate(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return AIResponse(json.dumps({"summary": "fixture", "claims": [], "proposals": [self.decision]}), 500, 200, self.model)


def world(tmp_path, now: datetime, **settings_overrides):
    engine = create_engine(f"sqlite:///{tmp_path}/cycle.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(**{"ai_trade_review": "off", "scanner_timeframes": "1h", **settings_overrides})
    last_open = last_closed_open("1h", now, settings.candle_close_grace_seconds)
    data = frame(last_open)
    return engine, factory, settings, data, last_open


async def prepare(factory, settings, data, last_open):
    with factory() as session:
        provider = FakeUniverseProvider([instrument("SOLUSDT", "SOL")], {"SOLUSDT": liquid("SOLUSDT", float(data.close.iloc[-1]))})
        await UniverseService(session, settings, provider).refresh("1h")
        version = validated_version(session, "SOLUSDT", payload=version_payload(parse_spec(SPEC), "cycle fixture"))
        session.add(MarketRegimeRecord(exchange="binance", symbol="SOLUSDT", timeframe="4h", regime="TRENDING_UP",
                                       confidence=0.8, candle_timestamp=last_open - 4 * HOUR))
        session.commit()
        return version.id


def closes(data: pd.DataFrame):
    latest = {"SOLUSDT": float(data.close.iloc[-1])}
    return latest, FeedQuotes(latest.get)


def test_latest_closed_candle_excludes_the_forming_candle():
    now = datetime(2026, 9, 26, 12, 0, 3, tzinfo=UTC)
    assert last_closed_open("1h", now) == datetime(2026, 9, 26, 11, tzinfo=UTC)
    assert last_closed_open("1h", now, grace_seconds=5) == datetime(2026, 9, 26, 10, tzinfo=UTC)  # 12:00 candle not final yet
    assert last_closed_open("15m", datetime(2026, 9, 26, 12, 14, 59, tzinfo=UTC)) == datetime(2026, 9, 26, 11, 45, tzinfo=UTC)


def test_conservative_ohlc_exit_rules():
    candle = lambda o, h, low, c: MarketCandle(open=o, high=h, low=low, close=c, volume=1)  # noqa: E731
    stop, target = Decimal("97"), Decimal("106")
    assert candle_exit("BUY", stop, target, candle(95, 99, 94, 98)) == ("stop_loss_gap", Decimal("95"))
    assert candle_exit("BUY", stop, target, candle(100, 107, 96, 104)) == ("stop_loss_ambiguous_candle", stop)
    assert candle_exit("BUY", stop, target, candle(100, 107, 99, 105)) == ("take_profit", target)
    assert candle_exit("BUY", stop, target, candle(108, 110, 107, 109)) == ("take_profit", target)  # no price improvement
    assert candle_exit("BUY", stop, target, candle(100, 105, 98, 101)) is None


@pytest.mark.asyncio
async def test_rest_cycle_trades_once_per_candle_monitors_and_survives_restart(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    version_id = await prepare(factory, settings, data, last_open)
    latest, quotes = closes(data)
    candles = FrameCandles({"SOLUSDT": data})
    cycle = ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now)

    first = await cycle.run()
    decision = first["timeframes"]["1h"]["decision_details"][0]
    assert decision["status"] == "DECIDED" and decision["outcome"] == "TRADE" and decision.get("position_id"), first
    with factory() as session:
        order = session.scalar(select(Order))
        position = session.get(Position, decision["position_id"])
        assert order.execution_mode == "PAPER" and order.market_data_source == "FIXTURE" and order.status == "FILLED"
        # BUY fills at the quoted ask plus configured slippage; slippage is accounted.
        ask = Decimal(str(latest["SOLUSDT"])) * Decimal("1.0001")
        assert Decimal(order.average_fill_price) >= ask and Decimal(order.slippage_cost) > 0
        assert position.timeframe == "1h" and position.strategy_version_id == version_id
        assert position.last_evaluated_candle_at.replace(tzinfo=UTC) == last_open
        rationale = session.get(Decision, decision["decision_id"]).rationale
        assert rationale["candle"]["open_time"] == last_open.isoformat() and rationale["risk"]["outcome"] == "APPROVED"
        assert rationale["plan"]["stop_loss"] < rationale["plan"]["entry_reference"] < rationale["plan"]["take_profit"][0]

    # Same candle again, then after a "restart" (new cycle object): nothing is duplicated.
    again = await cycle.run()
    assert again["timeframes"]["1h"]["decisions"] == {"ALREADY_PROCESSED": 1}
    restarted = ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now)
    await restarted.run()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(Decision)) == 1
        assert session.scalar(select(func.count()).select_from(ProcessedCandle).where(ProcessedCandle.symbol == "SOLUSDT")) == 1

    # Next hour: the new candle trades through the stop -> REST monitoring closes the position.
    next_open = last_open + HOUR
    entry = float(order.average_fill_price)
    crash = pd.DataFrame([{"timestamp": pd.Timestamp(next_open), "open": entry * 0.999, "high": entry * 1.001,
                           "low": entry * 0.95, "close": entry * 0.96, "volume": 1500.0}])
    candles.frames["SOLUSDT"] = pd.concat([data, crash], ignore_index=True)
    latest["SOLUSDT"] = entry * 0.96
    later = ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + HOUR)
    report = await later.run()
    closed = report["timeframes"]["1h"]["monitoring"]["closed"]
    assert closed and closed[0]["reason"] == "stop_loss" and closed[0]["trigger_source"] == "CANDLE_OHLC"
    with factory() as session:
        position = session.get(Position, decision["position_id"])
        assert position.status == "CLOSED" and position.exit_price < position.stop_loss  # stop + slippage
        assert Decimal(position.realized_pnl) < 0 and Decimal(position.fees) > 0
        trade = session.scalar(select(TradeMemory).where(TradeMemory.position_id == position.id))
        analysis = session.scalar(select(PostTradeAnalysis).where(PostTradeAnalysis.trade_memory_id == trade.id))
        assert analysis.expected["planned"]["stop_loss"] and analysis.actual["exit_reason"] == "stop_loss"
        account = PaperAccount(session, settings).summary()
        # Equity is derived from positions only: starting + realized - fees + unrealized (the fixture
        # entry rule is always true, so the next candle legitimately opened a new position).
        positions = session.scalars(select(Position)).all()
        expected_equity = Decimal(settings.starting_equity) + sum(
            Decimal(p.realized_pnl) - Decimal(p.fees) + (Decimal(p.current_price) - Decimal(p.entry_price)) * Decimal(p.quantity)
            * (p.status != "CLOSED") for p in positions)
        assert abs(Decimal(account["equity"]) - expected_equity) < Decimal("0.01")
        assert account["trades_closed"] == 1 and account["losing_trades"] == 1
        assert account["open_positions"] == sum(1 for p in positions if p.status != "CLOSED")
        assert session.scalar(select(func.count()).select_from(PortfolioSnapshot)) >= 3  # equity curve
    engine.dispose()


@pytest.mark.asyncio
async def test_target_and_time_exits(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)
    latest, quotes = closes(data)
    candles = FrameCandles({"SOLUSDT": data})
    first = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now).run()
    position_id = first["timeframes"]["1h"]["decision_details"][0]["position_id"]
    with factory() as session:
        position = session.get(Position, position_id)
        target, entry = float(position.take_profit), float(position.entry_price)
    rally = pd.DataFrame([{"timestamp": pd.Timestamp(last_open + HOUR), "open": entry, "high": target * 1.01,
                           "low": entry * 0.995, "close": target * 1.005, "volume": 900.0}])
    candles.frames["SOLUSDT"] = pd.concat([data, rally], ignore_index=True)
    latest["SOLUSDT"] = target * 1.005
    report = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + HOUR).run()
    closed = report["timeframes"]["1h"]["monitoring"]["closed"][0]
    assert closed["reason"] == "take_profit" and Decimal(closed["exit_price"]) == Decimal(str(target)).quantize(Decimal(closed["exit_price"]))
    # Timeout: a position with a 1-bar maximum holding period exits on the next closed candle.
    with factory() as session:
        version = session.get(Position, position_id).strategy_version_id
        held = Position(strategy_version_id=version, symbol="SOLUSDT", side="BUY", quantity=1, entry_price=entry,
                        current_price=entry, stop_loss=entry * 0.5, take_profit=entry * 2, status="OPEN", venue="paper",
                        timeframe="1h", market_exchange="binance", max_holding_bars=1,
                        last_evaluated_candle_at=last_open + HOUR)
        session.add(held)
        session.commit()
        held_id = held.id
    flat = pd.DataFrame([{"timestamp": pd.Timestamp(last_open + 2 * HOUR), "open": entry, "high": entry * 1.001,
                          "low": entry * 0.999, "close": entry, "volume": 900.0}])
    candles.frames["SOLUSDT"] = pd.concat([candles.frames["SOLUSDT"], flat], ignore_index=True)
    report = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + 2 * HOUR).run()
    assert any(item["position_id"] == held_id and item["reason"] == "max_holding_period"
               for item in report["timeframes"]["1h"]["monitoring"]["closed"])
    engine.dispose()


@pytest.mark.asyncio
async def test_required_ai_no_trade_failure_and_risk_cannot_be_bypassed(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now, ai_trade_review="required")
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    candles = FrameCandles({"SOLUSDT": data})

    # Gemini unavailable (503) -> NO_TRADE; nothing is fabricated.
    from app.ai.providers import AIProviderError

    down = FixtureAI(error=AIProviderError("503 high demand", retryable=False))
    report = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now, ai_provider=down).run()
    detail = report["timeframes"]["1h"]["decision_details"][0]
    assert detail["outcome"] == "WAIT" and "Gemini decision required" in detail["reasoning"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0

    # Next candle: Gemini says TRADE, but an ASSET kill switch is active -> Risk rejects; AI cannot override.
    candles.frames["SOLUSDT"] = pd.concat([data, frame(last_open + HOUR, 1, float(data.close.iloc[-1]))], ignore_index=True)
    with factory() as session:
        SafetyService(session).activate("ASSET", "operator hold", target="SOLUSDT")
        session.commit()
    close = float(data.close.iloc[-1])
    yes = FixtureAI({"decision": "TRADE", "direction": "LONG", "entry": close, "stop_loss": close * 0.97,
                     "take_profit": close * 1.06, "thesis": "trend", "invalidation": "close below stop", "confidence": 0.7})
    report = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + HOUR, ai_provider=yes).run()
    detail = report["timeframes"]["1h"]["decision_details"][0]
    assert yes.calls == 1, detail
    assert detail["risk_outcome"] == "REJECTED" and "SAFETY_ASSET_STOP" in detail["risk_reasons"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_no_lookahead_future_candle_news_and_macro_are_invisible(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    decision_time = last_open + HOUR
    with factory() as session:
        # A forming/future candle that is already in the database must not be used.
        session.add(MarketCandle(exchange="binance", symbol="SOLUSDT", timeframe="1h", timestamp=decision_time,
                                 open=1, high=1, low=1, close=1, volume=1, is_demo=False))
        # A hack published after the decision time must not block (or appear in) this decision.
        session.add(MarketEvent(category="CRYPTO", event_type="HACK", title="future exploit", severity="HIGH", source="finnhub",
                                description="d", affected_assets=["SOL"], verification_status="SOURCE_VERIFIED",
                                event_at=decision_time + timedelta(minutes=10), available_at=decision_time + timedelta(minutes=10)))
        # A FRED vintage released after the decision time must not be visible.
        MacroService(session, settings).store(parse_vintages("DGS10", {"observations": [
            {"date": decision_time.date().isoformat(), "value": "9.99", "realtime_start": (decision_time + timedelta(days=1)).date().isoformat(),
             "realtime_end": "9999-12-31"}]}, "Percent", "10Y"))
        session.commit()
    report = await ClosedCandleCycle(factory, settings, EventBus(), FrameCandles({"SOLUSDT": data}), quotes, clock=lambda: now).run()
    detail = report["timeframes"]["1h"]["decision_details"][0]
    with factory() as session:
        rationale = session.get(Decision, detail["decision_id"]).rationale
        assert rationale["candle"]["open_time"] == last_open.isoformat()
        assert not rationale["blocking_evidence"] and rationale["news_context"] == []
        assert "DGS10" not in rationale["macro_context"]
        assert float(session.scalar(select(Order.reference_price))) != 1.0  # the future candle's close was not used
    engine.dispose()


@pytest.mark.asyncio
async def test_provider_failures_mean_no_trade(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)

    class Broken:
        name = "binance"

        async def fetch_batch(self, *args):
            raise httpx.ConnectError("unreachable")

    from paper_fixtures import FixedQuotes

    no_quotes = FixedQuotes(0, 0, fail=True)
    report = await ClosedCandleCycle(factory, settings, EventBus(), Broken(), no_quotes, clock=lambda: now).run()
    timeframe = report["timeframes"]["1h"]
    assert "SOLUSDT" in timeframe["sync"]["errors"]
    assert timeframe["decisions"] == {"CANDLE_NOT_AVAILABLE": 1}  # no stored candle -> not claimed, retried later
    # With candles stored but no quote, Risk rejects: a fill is never priced without real bid/ask.
    report = await ClosedCandleCycle(factory, settings, EventBus(), FrameCandles({"SOLUSDT": data}), no_quotes, clock=lambda: now).run()
    detail = report["timeframes"]["1h"]["decision_details"][0]
    assert detail["risk_outcome"] == "REJECTED" and "QUOTE_UNAVAILABLE" in detail["risk_reasons"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_binance_quote_client_rejects_bad_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        if symbol == "GEO":
            return httpx.Response(451, json={"msg": "restricted location"})
        if symbol == "BAD":
            return httpx.Response(200, json={"bidPrice": "10", "askPrice": "9", "symbol": "BAD"})
        if symbol == "JUNK":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"symbol": symbol, "bidPrice": "100.0", "askPrice": "100.02"})

    client = BinanceQuoteProvider("https://data-api.binance.vision", transport=httpx.MockTransport(handler))
    quote = await client.quote("SOLUSDT")
    assert quote.source == "REAL_BINANCE" and quote.bid == Decimal("100.0") and round(float(quote.spread_bps), 2) == 2.0
    for symbol in ("GEO", "BAD", "JUNK"):
        with pytest.raises(QuoteUnavailable):
            await client.quote(symbol)


@pytest.mark.asyncio
async def test_emergency_stop_halts_decisions_but_keeps_monitoring(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    with factory() as session:
        SafetyService(session).activate("EMERGENCY", "operator emergency stop")
        session.commit()
    report = await ClosedCandleCycle(factory, settings, EventBus(), FrameCandles({"SOLUSDT": data}), quotes, clock=lambda: now).run()
    timeframe = report["timeframes"]["1h"]
    assert "SAFETY_EMERGENCY_STOP" in timeframe["decisions"]["skipped"] and "monitoring" in timeframe
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Decision)) == 0
    engine.dispose()


def test_strategy_without_complete_validation_is_not_paper_eligible(session):
    from app.models import Strategy, StrategyVersion, ValidationResult
    from app.trading.eligibility import check

    strategy = Strategy(name="one backtest", symbol="BTCUSDT", timeframe="1h", status="paper_testing")
    session.add(strategy)
    session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version=1, content_hash="x" * 64, parameters={}, entry_rules=[],
                              exit_rules=[], filters={}, risk_assumptions={}, documentation="d")
    session.add(version)
    session.flush()
    session.add(ValidationResult(strategy_version_id=version.id, method="backtest", result="PASS", metrics={"net_profit": 1e6}))
    session.flush()
    verdict = check(session, Settings(), version)
    assert not verdict.eligible and any(reason.startswith("MISSING_VALIDATION") for reason in verdict.reasons)
    strategy.status = "retired"
    assert "STATUS_RETIRED_NOT_PAPER_ELIGIBLE" in check(session, Settings(), version).reasons


@pytest.mark.asyncio
async def test_concurrent_cycles_process_each_candle_once(tmp_path):
    """Two schedulers (e.g. two processes) racing on the same candle: the DB ledger admits one."""
    import asyncio

    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    with factory() as session:  # candles already synced so both cycles go straight to deciding
        for row in data.itertuples():
            session.add(MarketCandle(exchange="binance", symbol="SOLUSDT", timeframe="1h", timestamp=row.timestamp.to_pydatetime(),
                                     open=row.open, high=row.high, low=row.low, close=row.close, volume=row.volume, is_demo=False))
        session.commit()
    cycles = [ClosedCandleCycle(factory, settings, EventBus(), FrameCandles({"SOLUSDT": data}), quotes, clock=lambda: now)
              for _ in range(2)]
    await asyncio.gather(*(cycle.run() for cycle in cycles))
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Decision)) == 1
        assert session.scalar(select(func.count()).select_from(Order)) == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_records_failure_and_retries_on_next_interval():
    import asyncio

    from app.core.orchestrator import TaskOrchestrator

    attempts = {"n": 0}

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("provider down")

    orchestrator = TaskOrchestrator()
    orchestrator.schedule("flaky", 1, flaky, initial_delay=0.01)
    for _ in range(300):
        if attempts["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    state = orchestrator.states["flaky"]
    await orchestrator.stop()
    assert attempts["n"] >= 2 and state.runs >= 1 and state.status in {"HEALTHY", "RUNNING"}


def test_higher_timeframe_context_uses_only_fully_closed_buckets():
    from app.trading.candle_cycle import resample_closed

    end = datetime(2026, 9, 26, 10, tzinfo=UTC)  # last closed 1h candle opens 10:00 -> closes 11:00
    data = frame(end, n=12)
    four_hour = resample_closed(data, "1h", "4h", end + HOUR)
    # 08:00-12:00 bucket is still forming at 11:00 and is excluded; complete buckets only.
    assert list(four_hour["timestamp"].dt.hour) == [0, 4]
    first = data[(data.timestamp >= pd.Timestamp("2026-09-26T00:00Z")) & (data.timestamp < pd.Timestamp("2026-09-26T04:00Z"))]
    assert four_hour.iloc[0]["high"] == first["high"].max() and four_hour.iloc[0]["close"] == first["close"].iloc[-1]
