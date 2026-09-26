"""DETERMINISTIC FIXTURE TESTS for the 1-month paper test readiness audit:
complete trade records, the NO_TRADE journal, fixed paper capital and portfolio risk.
Fixture candles/quotes/AI answers only — none of this is a real-market trade."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from paper_fixtures import FixedQuotes, validated_version
from sqlalchemy import select
from test_candle_cycle import HOUR, FixtureAI, FrameCandles, closes, prepare, world

from app.api_autonomy import decision_journal, paper_trade_record
from app.core.config import Settings
from app.core.events import EventBus
from app.models import Decision, ExecutionPlan, PaperAccountRecord, Position, TradeIntent
from app.trading.candle_cycle import ClosedCandleCycle
from app.trading.paper_account import PaperAccount
from app.trading.paper_exchange import PaperExchange
from app.trading.risk import RiskEngine
from app.trading.safety import SafetyService


def trade_decision(close: float) -> dict:
    return {"decision": "TRADE", "direction": "LONG", "entry": close, "stop_loss": close * 0.97, "take_profit": close * 1.06,
            "thesis": "trend continuation", "invalidation": "close below the stop", "confidence": 0.62,
            "evidence": ["support:0"], "reasons": ["setup and liquidity confirmed"]}


@pytest.mark.asyncio
async def test_closed_paper_trade_has_a_complete_audit_record(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now, ai_trade_review="required")
    await prepare(factory, settings, data, last_open)
    latest, quotes = closes(data)
    candles = FrameCandles({"SOLUSDT": data})
    ai = FixtureAI(trade_decision(float(data.close.iloc[-1])))
    first = await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now, ai_provider=ai).run()
    position_id = first["timeframes"]["1h"]["decision_details"][0]["position_id"]
    with factory() as session:
        entry = float(session.get(Position, position_id).entry_price)
    crash = pd.DataFrame([{"timestamp": pd.Timestamp(last_open + HOUR), "open": entry, "high": entry * 1.001,
                           "low": entry * 0.95, "close": entry * 0.96, "volume": 1500.0}])
    candles.frames["SOLUSDT"] = pd.concat([data, crash], ignore_index=True)
    latest["SOLUSDT"] = entry * 0.96
    await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + HOUR,
                            ai_provider=FixtureAI({"decision": "NO_TRADE", "reasons": ["fixture"]})).run()
    with factory() as session:
        record = paper_trade_record(position_id, session)
    identity = record["identity"]
    assert identity["strategy_version_id"] and identity["strategy_version"] == "1" or identity["strategy_version"] == 1
    assert identity["order_id"] and identity["symbol"] == "SOLUSDT" and identity["timeframe"] == "1h"
    candle = record["timing"]["candle"]
    assert {"open", "high", "low", "close", "volume", "open_time", "close_time"} <= set(candle)
    assert record["timing"]["entry_at"] and record["timing"]["exit_at"]
    assert record["market_state"]["market_regime"] or record["market_state"]["regime"]
    features = record["strategy_evidence"]["features"]
    assert "close" in features and features["close"]["last"] is not None  # the value the entry rule actually used
    assert record["strategy_evidence"]["conditions"][0]["result"] is True
    ai_record = record["ai_decision"]
    assert ai_record["decision"] == "TRADE" and ai_record["model"] == "fixture" and ai_record["stop_loss"]
    sizing = record["risk_decision"]["sizing"]
    for key in ("proposed_quantity", "approved_quantity", "risk_amount", "risk_pct_of_equity", "stop_distance_pct",
                "reward_risk", "exposure_before", "exposure_after", "equity"):
        assert sizing[key] is not None, key
    order = record["execution"]["order"]
    assert order["execution_mode"] == "PAPER" and order["bid"] and order["ask"] and Decimal(order["slippage_cost"]) > 0
    outcome = record["outcome"]
    assert outcome["exit_reason"] == "stop_loss" and outcome["result"] == "LOSS"
    assert Decimal(outcome["net_pnl"]) == Decimal(outcome["gross_pnl"]) - Decimal(outcome["fees"])
    assert outcome["mae_pct"] is not None and outcome["holding_seconds"] is not None
    assert record["post_trade_analysis"]["expected"]["planned"]["stop_loss"]
    engine.dispose()


@pytest.mark.asyncio
async def test_every_no_trade_has_an_auditable_reason(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 4 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now, ai_trade_review="required")
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    candles = FrameCandles({"SOLUSDT": data})
    close = float(data.close.iloc[-1])

    def extend(hours: int) -> None:
        extra = [{"timestamp": pd.Timestamp(last_open + HOUR * step), "open": close, "high": close * 1.001,
                  "low": close * 0.999, "close": close, "volume": 1000.0} for step in range(1, hours + 1)]
        candles.frames["SOLUSDT"] = pd.concat([data, pd.DataFrame(extra)], ignore_index=True)

    # 1) Gemini unavailable, 2) Gemini NO_TRADE, 3) Gemini TRADE but kill switch -> Risk rejects.
    await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now,
                            ai_provider=FixtureAI(error=ConnectionError("timeout"))).run()
    extend(1)
    await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + HOUR,
                            ai_provider=FixtureAI({"decision": "NO_TRADE", "reasons": ["BTC context weak"]})).run()
    with factory() as session:
        SafetyService(session).activate("ASSET", "hold", target="SOLUSDT")
        session.commit()
    extend(2)
    await ClosedCandleCycle(factory, settings, EventBus(), candles, quotes, clock=lambda: now + 2 * HOUR,
                            ai_provider=FixtureAI(trade_decision(close))).run()
    with factory() as session:
        journal = decision_journal(symbol="SOLUSDT", limit=10, session=session)["items"]
        codes = [item["reason_code"] for item in reversed(journal)]
        assert codes == ["AI_UNAVAILABLE", "AI_NO_TRADE", "RISK_REJECTED"]
        assert all(item["final_decision"] == "NO_TRADE" and item["market_state"]["close"] and item["candle_open_time"]
                   for item in journal)
        assert journal[0]["risk"]["reasons"] == ["SAFETY_ASSET_STOP"] and journal[1]["ai"]["decision"] == "NO_TRADE"
        assert journal[0]["strategies_evaluated"][0]["strategy_version_id"]
        # Filtering works for the monthly review.
        assert len(decision_journal(reason_code="AI_NO_TRADE", limit=10, session=session)["items"]) == 1
    engine.dispose()


def test_no_strategy_and_ineligible_strategy_are_distinguished(session):
    import asyncio

    from app.trading.pipeline import MarketSnapshot, TradingPipeline

    snapshot = MarketSnapshot("BTCUSDT", 100, 10, 0.2, 0, "TRENDING", "BULLISH", exchange="binance", timeframe="1h")
    result = asyncio.run(TradingPipeline(session, Settings(), EventBus()).evaluate(snapshot))
    assert session.get(Decision, result["decision_id"]).rationale["reason_code"] == "NO_STRATEGY_COVERS_ASSET"
    from app.models import ValidationResult

    version = validated_version(session, "BTCUSDT")  # paper_testing, but robustness later FAILED
    session.add(ValidationResult(strategy_version_id=version.id, method="robustness", result="FAIL", metrics={}))
    session.commit()
    result = asyncio.run(TradingPipeline(session, Settings(), EventBus()).evaluate(snapshot))
    rationale = session.get(Decision, result["decision_id"]).rationale
    assert rationale["reason_code"] == "STRATEGY_NOT_PAPER_ELIGIBLE" and rationale["market_state"]["close"] == 100
    evaluation = session.get(Decision, result["decision_id"]).evaluations[0]
    assert evaluation["strategy_version_id"] == version.id and evaluation["ineligibility_reasons"]


@pytest.mark.asyncio
async def test_paper_capital_is_fixed_at_100k_and_never_double_spent(session):
    account = PaperAccount(session, Settings())
    assert account.initial_capital() == Decimal("100000") and account.summary()["initial_balance"].startswith("100000")
    session.commit()
    # Editing STARTING_EQUITY later cannot create capital: the persisted value is authoritative.
    changed = PaperAccount(session, Settings(starting_equity=250_000)).summary()
    assert Decimal(changed["initial_balance"]) == Decimal("100000") and changed["configured_starting_equity_ignored"]
    assert session.scalar(select(PaperAccountRecord.initial_capital)) == Decimal("100000")

    def plan(qty: float, px: float):
        version = validated_version(session, "BTCUSDT")
        decision = Decision(symbol="BTCUSDT", outcome="TRADE", market_context={}, evaluations=[], confidence=1,
                            reasoning=["t"], correlation_id="c")
        session.add(decision)
        session.flush()
        intent = TradeIntent(decision_id=decision.id, strategy_version_id=version.id, symbol="BTCUSDT", side="BUY",
                             entry_price=px, confidence=1)
        session.add(intent)
        session.flush()
        item = ExecutionPlan(trade_intent_id=intent.id, exchange="paper", symbol="BTCUSDT", side="BUY", quantity=qty,
                             order_type="MARKET", status="approved", risk_amount=1, stop_loss=px * 0.97, take_profit=px * 1.06,
                             leverage=1)
        session.add(item)
        session.flush()
        return item, intent

    quotes = FixedQuotes(59_999.9, 60_000.0)
    exchange = PaperExchange(session, Settings(), EventBus(), quote=await quotes.quote("BTCUSDT"))
    first_plan, first_intent = plan(0.7, 60_000)  # ~$42k each
    assert (await exchange.submit(first_plan, first_intent, 60_000, "a")).get("position_id")
    second_plan, second_intent = plan(0.7, 60_000)
    assert (await exchange.submit(second_plan, second_intent, 60_000, "b")).get("position_id")
    # A third $42k purchase needs more cash than remains: rejected, no capital is created.
    third_plan, third_intent = plan(0.7, 60_000)
    rejected = await exchange.submit(third_plan, third_intent, 60_000, "c")
    assert rejected.get("rejected") and "INSUFFICIENT_BALANCE" in rejected["errors"]
    totals = PaperAccount(session, Settings()).totals()
    positions = session.scalars(select(Position)).all()
    cost = sum(Decimal(p.entry_price) * Decimal(p.quantity) for p in positions)
    fees = sum(Decimal(p.fees) for p in positions)
    assert len(positions) == 2 and totals["cash"] == Decimal("100000") - cost - fees and totals["cash"] >= 0
    summary = PaperAccount(session, Settings()).summary()
    for key in ("initial_balance", "cash_available", "reserved_capital", "open_exposure", "equity", "realized_pnl",
                "unrealized_pnl", "fees_paid", "slippage_cost", "cumulative_return_pct", "max_drawdown"):
        assert summary[key] is not None, key
    assert Decimal(summary["cash_available"]) == totals["cash"].quantize(Decimal("0.0000000001"))
    assert Decimal(summary["reserved_capital"]) == cost.quantize(Decimal("0.0000000001"))
    assert totals["equity"] == totals["cash"] + sum(Decimal(p.current_price) * Decimal(p.quantity) for p in positions)


def test_portfolio_risk_to_stops_is_capped(session):
    from app.models import AssetEligibility
    from app.trading.risk import MarketFacts

    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    session.add(AssetEligibility(run_id="r", exchange="binance", symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
                                 eligible=True, metrics={"spread_bps": 1, "quote_volume_24h": 1e9}, evaluated_at=now))
    version = validated_version(session, "ETHUSDT")
    # Open positions already risking 4.8% of equity to their stops.
    session.add(Position(strategy_version_id=version.id, symbol="ETHUSDT", side="BUY", quantity=48, entry_price=1000,
                         current_price=1000, stop_loss=900, take_profit=1300, status="OPEN", venue="paper"))
    decision = Decision(symbol="BTCUSDT", outcome="TRADE", market_context={}, evaluations=[], confidence=1, reasoning=["t"],
                        correlation_id="c")
    session.add(decision)
    session.flush()
    intent = TradeIntent(decision_id=decision.id, strategy_version_id=validated_version(session, "BTCUSDT").id,
                         symbol="BTCUSDT", side="BUY", entry_price=100, confidence=1)
    session.add(intent)
    session.commit()
    facts = MarketFacts("binance", "1h", now, now - timedelta(hours=1))
    result = RiskEngine(session, Settings()).assess(intent, 100, 0.2, facts)
    assert "PORTFOLIO_RISK_LIMIT" in result.reason_codes
    assert Decimal(result.details["open_portfolio_risk_before"]) == 4800


@pytest.mark.asyncio
async def test_interrupted_cycle_is_closed_out_not_replayed(tmp_path):
    """A process killed between claiming a candle and finishing it must not trade that candle later."""
    from sqlalchemy import func

    from app.core.time import TimeService
    from app.models import Order, ProcessedCandle

    now = datetime.now(UTC).replace(minute=0, second=30, microsecond=0) - 3 * HOUR
    engine, factory, settings, data, last_open = world(tmp_path, now)
    await prepare(factory, settings, data, last_open)
    _, quotes = closes(data)
    with factory() as session:  # simulated crash: claim written, decision never completed
        session.add(ProcessedCandle(exchange="binance", symbol="SOLUSDT", timeframe="1h", candle_open_time=last_open,
                                    candle_close_time=last_open + HOUR, status="CLAIMED",
                                    processed_at=TimeService.now() - timedelta(minutes=30)))
        session.commit()
    report = await ClosedCandleCycle(factory, settings, EventBus(), FrameCandles({"SOLUSDT": data}), quotes, clock=lambda: now).run()
    assert report["interrupted_marked"] == 1 and report["timeframes"]["1h"]["decisions"] == {"ALREADY_PROCESSED": 1}
    with factory() as session:
        assert session.scalar(select(ProcessedCandle.status).where(ProcessedCandle.symbol == "SOLUSDT")) == "INTERRUPTED"
        assert session.scalar(select(func.count()).select_from(Order)) == 0
    engine.dispose()
