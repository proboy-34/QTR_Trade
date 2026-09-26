"""Deterministic tests for evidence reasoning, independent risk gates, venue separation and the
Binance Spot Testnet adapter (mocked transport; nothing here touches a real exchange)."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from paper_fixtures import validated_version
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.db import Base
from app.execution.binance_testnet import TESTNET_HOST, BinanceTestnetExchange, client_order_id
from app.models import (
    AssetEligibility,
    Decision,
    ExecutionPlan,
    MarketEvent,
    MarketRegimeRecord,
    Order,
    PortfolioSnapshot,
    Position,
    Strategy,
    StrategyVersion,
    TradeIntent,
)
from app.trading.accounts import latest_portfolio, venue_for
from app.trading.pipeline import MarketSnapshot
from app.trading.reasoning import DecisionReasoner
from app.trading.risk import MarketFacts, RiskEngine
from app.trading.safety import SafetyService

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
TESTNET = {"execution_mode": "testnet", "binance_testnet_api_key": "tn-key-1234", "binance_testnet_api_secret": "tn-secret-5678"}


def strategy(session, symbol="SOLUSDT"):
    item = Strategy(name="EMA trend", symbol=symbol, timeframe="1h", status="paper_testing", description="EMA cross")
    session.add(item)
    session.flush()
    version = StrategyVersion(strategy_id=item.id, version=1, parameters={}, entry_rules=[], exit_rules=[], filters={},
                              risk_assumptions={}, documentation="t", content_hash=hashlib.sha256(symbol.encode()).hexdigest())
    session.add(version)
    session.flush()
    return item, version


def regime(session, symbol, timeframe, name, confidence=0.8, age=timedelta(hours=5)):
    session.add(MarketRegimeRecord(exchange="binance", symbol=symbol, timeframe=timeframe, regime=name,
                                   confidence=confidence, candle_timestamp=NOW - age))


def eligible(session, symbol="SOLUSDT", ok=True, spread=2.0, volume=5e8):
    session.add(AssetEligibility(run_id="run-1", exchange="binance", symbol=symbol, base_asset=symbol[:-4], quote_asset="USDT",
                                 eligible=ok, metrics={"spread_bps": spread, "quote_volume_24h": volume}, evaluated_at=NOW))


def snapshot(symbol="SOLUSDT", market_regime="TRENDING_UP"):
    return MarketSnapshot(symbol, 150, 0, 0.2, 0, "trending", "BULLISH", liquidity="UNKNOWN", observed_at=NOW,
                          exchange="binance", timeframe="1h", market_regime=market_regime)


def test_supported_setup_is_a_proposal_with_full_rationale(session):
    item, version = strategy(session)
    regime(session, "SOLUSDT", "4h", "TRENDING_UP")
    regime(session, "BTCUSDT", "1h", "TRENDING_UP")
    eligible(session)
    session.commit()
    result = DecisionReasoner(session, Settings()).assess(snapshot(), item, version, "live")
    assert result["decision"] == "TRADE_PROPOSAL" and result["blocking_evidence"] == []
    codes = {entry["code"] for entry in result["supporting_evidence"]}
    assert {"SETUP", "REGIME", "HIGHER_TIMEFRAME", "BTC_CONTEXT", "LIQUIDITY"} <= codes
    for field in ("asset", "direction", "decision_time", "timeframe_roles", "strategy", "thesis", "invalidation",
                  "time_horizon", "evidence_strength", "data_sources", "macro_context", "risk_config_version"):
        assert field in result
    assert result["timeframe_roles"]["context"] == "4h" and result["strategy"]["version"] == 1


def test_higher_timeframe_downtrend_and_btc_panic_block(session):
    item, version = strategy(session)
    regime(session, "SOLUSDT", "4h", "TRENDING_DOWN", confidence=0.9)
    regime(session, "BTCUSDT", "1h", "PANIC")
    eligible(session)
    session.commit()
    result = DecisionReasoner(session, Settings()).assess(snapshot(), item, version, "live")
    assert result["decision"] == "NO_TRADE"
    assert {entry["code"] for entry in result["blocking_evidence"]} == {"HIGHER_TIMEFRAME", "BTC_PANIC"}


def test_regime_computed_after_decision_time_is_not_used(session):
    item, version = strategy(session)
    regime(session, "SOLUSDT", "4h", "TRENDING_DOWN", age=timedelta(hours=-4))  # a future candle: must be ignored
    session.commit()
    result = DecisionReasoner(session, Settings()).assess(snapshot(), item, version, "live")
    assert result["market_regime"]["context"] is None and not result["blocking_evidence"]


def test_trusted_hack_blocks_unverified_rumour_does_not_and_future_news_is_invisible(session):
    item, version = strategy(session)
    base = {"category": "CRYPTO", "severity": "HIGH", "source": "finnhub", "description": "d", "affected_assets": ["SOL"]}
    session.add(MarketEvent(event_type="OTHER", title="rumour", verification_status="UNVERIFIED",
                            event_at=NOW - timedelta(hours=1), available_at=NOW - timedelta(hours=1), **base))
    session.add(MarketEvent(event_type="HACK", title="published later", verification_status="SOURCE_VERIFIED",
                            event_at=NOW - timedelta(hours=1), available_at=NOW + timedelta(minutes=5), **base))
    session.commit()
    reasoner = DecisionReasoner(session, Settings())
    assert not reasoner.assess(snapshot(), item, version, "live")["blocking_evidence"]
    session.add(MarketEvent(event_type="HACK", title="bridge exploit", verification_status="SOURCE_VERIFIED",
                            event_at=NOW - timedelta(hours=2), available_at=NOW - timedelta(hours=2), **base))
    session.commit()
    result = reasoner.assess(snapshot(), item, version, "live")
    assert result["decision"] == "NO_TRADE" and result["blocking_evidence"][0]["code"] == "EVENT_RISK"


def test_missing_context_outweighs_a_bare_setup(session):
    item, version = strategy(session)
    result = DecisionReasoner(session, Settings()).assess(snapshot(market_regime="UNKNOWN"), item, version, "live")
    assert result["decision"] == "NO_TRADE" and "outweighs" in result["reason"]
    assert {"REGIME_UNCLEAR", "CONTEXT_UNAVAILABLE", "LIQUIDITY"} <= {e["code"] for e in result["contradicting_evidence"]}


def make_intent(session, symbol="BTCUSDT") -> TradeIntent:
    decision = Decision(symbol=symbol, outcome="TRADE", market_context={}, evaluations=[], confidence=1,
                        reasoning=["t"], correlation_id="c")
    session.add(decision)
    session.flush()
    intent = TradeIntent(decision_id=decision.id, strategy_version_id=validated_version(session, symbol).id, symbol=symbol,
                         side="BUY", entry_price=100, confidence=1)
    session.add(intent)
    session.flush()
    return intent


def facts(last_candle_age=timedelta(hours=1), exchange="binance"):
    return MarketFacts(exchange, "1h", NOW, NOW - last_candle_age if last_candle_age is not None else None)


def test_risk_gates_reject_stale_missing_illiquid_and_wide_spread(session):
    engine = RiskEngine(session, Settings())
    assert {"STALE_MARKET_DATA", "NO_LIQUIDITY_EVIDENCE"} <= set(engine.assess(make_intent(session), 100, 0.2, facts(timedelta(hours=9))).reason_codes)
    assert "NO_MARKET_DATA" in engine.assess(make_intent(session), 100, 0.2, facts(None)).reason_codes
    eligible(session, "BTCUSDT", ok=False, spread=80, volume=10)
    session.commit()
    codes = set(engine.assess(make_intent(session), 100, 0.2, facts()).reason_codes)
    assert {"ASSET_NOT_ELIGIBLE", "SPREAD_TOO_WIDE_OR_UNKNOWN", "INSUFFICIENT_LIQUIDITY"} <= codes


def test_risk_approves_fresh_liquid_data_and_enforces_reward_risk(session):
    eligible(session, "BTCUSDT")
    session.commit()
    assert RiskEngine(session, Settings()).assess(make_intent(session), 100, 0.2, facts()).approved
    rejected = RiskEngine(session, Settings(min_reward_risk=3)).assess(make_intent(session), 100, 0.2, facts())
    assert rejected.reason_codes == ["REWARD_RISK_BELOW_MINIMUM"]


def test_live_disabled_mode_has_no_venue_and_rejects_everything(session):
    settings = Settings(execution_mode="live_disabled")
    assert venue_for(settings) == "none"
    assert "EXECUTION_DISABLED" in RiskEngine(session, settings).assess(make_intent(session), 100, 0.2).reason_codes
    with pytest.raises(ValueError):
        Settings(execution_mode="live")
    with pytest.raises(ValueError):
        Settings(execution_mode="testnet")  # testnet requires its own separate keys


def test_kill_switch_survives_a_restart_and_scopes_apply(tmp_path):
    url = f"sqlite:///{tmp_path}/restart.db"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as first:
        SafetyService(first).activate("NEW_ORDERS", "operator halt")
        SafetyService(first).activate("ASSET", "delisting risk", target="SOLUSDT")
        first.commit()
    engine.dispose()
    with sessionmaker(bind=create_engine(url))() as restarted:  # a new process sees the same controls
        blocks = SafetyService(restarted).blocks_new_orders(None, "SOLUSDT")
        assert blocks and any("NEW_ORDERS" in code for code in blocks) and any("ASSET" in code for code in blocks)


def test_paper_and_testnet_portfolios_never_mix(session):
    session.add(PortfolioSnapshot(venue="paper", equity=100_000, available_balance=100_000, exposure=0, margin_used=0, daily_pnl=0, drawdown=0))
    session.add(PortfolioSnapshot(venue="testnet", equity=500, available_balance=500, exposure=0, margin_used=0, daily_pnl=0, drawdown=0))
    session.commit()
    assert float(latest_portfolio(session, "paper").equity) == 100_000
    assert float(latest_portfolio(session, "testnet").equity) == 500


class FakeTestnet:
    def __init__(self, reject: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.reject = reject

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url).startswith(TESTNET_HOST) and request.headers["X-MBX-APIKEY"] == "tn-key-1234"
        params = parse_qs(request.url.query.decode())
        assert "signature" in params and "timestamp" in params
        if request.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": "1000", "locked": "0"}]})
        if request.method == "POST" and self.reject:
            return httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance."})
        if request.method == "POST":
            return httpx.Response(200, json={"orderId": 42 + len(self.requests), "status": "FILLED", "fills": [
                {"qty": params["quantity"][0], "price": "100.5", "commission": "0.01", "commissionAsset": "USDT", "tradeId": len(self.requests)}]})
        return httpx.Response(200, json={"status": "FILLED", "executedQty": "0.5"})


def testnet_plan(session):
    intent = make_intent(session)
    plan = ExecutionPlan(trade_intent_id=intent.id, exchange="testnet", symbol="BTCUSDT", side="BUY", quantity=0.5,
                         order_type="MARKET", status="approved", risk_amount=5, stop_loss=95, take_profit=110, leverage=1)
    session.add(plan)
    session.flush()
    return plan, intent


def test_testnet_adapter_refuses_without_mode_or_keys(session):
    with pytest.raises(SafetyError):
        BinanceTestnetExchange(session, Settings(), EventBus())
    assert len(client_order_id("qtr", "12345678-1234-1234-1234-123456789012")) <= 36


@pytest.mark.asyncio
async def test_testnet_submit_is_idempotent_and_records_testnet_venue(session):
    fake = FakeTestnet()
    exchange = BinanceTestnetExchange(session, Settings(**TESTNET), EventBus(), httpx.MockTransport(fake))
    snapshot_row = await exchange.sync_portfolio()
    assert snapshot_row.venue == "testnet" and float(snapshot_row.equity) == 1000
    plan, intent = testnet_plan(session)
    first = await exchange.submit(plan, intent, 100, "corr")
    second = await exchange.submit(plan, intent, 100, "corr")
    assert second["idempotent"] and first["order_id"] == second["order_id"]
    assert sum(1 for request in fake.requests if request.method == "POST") == 1
    position = session.get(Position, first["position_id"])
    assert position.venue == "testnet" and float(position.entry_price) == 100.5
    order = session.get(Order, first["order_id"])
    assert order.status == "FILLED" and "tn-secret-5678" not in json.dumps(order.raw_response)
    assert await exchange.close(position, "stop") == pytest.approx(100.5, rel=1e-9)
    order.status = "NEW"
    assert (await exchange.reconcile())["orders_checked"] == 1
    assert session.scalar(select(func.count()).select_from(Position).where(Position.venue == "paper")) == 0


@pytest.mark.asyncio
async def test_testnet_rejection_opens_nothing_and_hides_credentials(session):
    exchange = BinanceTestnetExchange(session, Settings(**TESTNET), EventBus(), httpx.MockTransport(FakeTestnet(reject=True)))
    plan, intent = testnet_plan(session)
    result = await exchange.submit(plan, intent, 100, "corr")
    assert result["rejected"] and "insufficient balance" in result["errors"][0]
    assert "tn-secret-5678" not in result["errors"][0] and "tn-key-1234" not in result["errors"][0]
    assert session.scalar(select(func.count()).select_from(Position)) == 0
    paper_plan = ExecutionPlan(trade_intent_id=intent.id, exchange="paper", symbol="BTCUSDT", side="BUY", quantity=1,
                               order_type="MARKET", status="approved", risk_amount=1, stop_loss=95, take_profit=110, leverage=1)
    with pytest.raises(SafetyError):
        await exchange.submit(paper_plan, intent, 100, "corr")  # never executes another venue's plan


def test_risk_quote_gates_and_lookahead_guard(session):
    from decimal import Decimal

    from app.global_services.quotes import Quote

    eligible(session, "BTCUSDT")
    session.commit()
    engine = RiskEngine(session, Settings())
    fresh = Quote("BTCUSDT", Decimal("100.00"), Decimal("100.01"), datetime.now(UTC))

    def with_quote(quote, **extra):
        return MarketFacts("binance", "1h", NOW, NOW - timedelta(hours=1), quote=quote, require_quote=True, **extra)

    assert engine.assess(make_intent(session), 100, 0.2, with_quote(fresh)).approved
    stale = Quote("BTCUSDT", Decimal("100.00"), Decimal("100.01"), datetime.now(UTC) - timedelta(minutes=5))
    assert "STALE_QUOTE" in engine.assess(make_intent(session), 100, 0.2, with_quote(stale)).reason_codes
    wide = Quote("BTCUSDT", Decimal("99"), Decimal("101"), datetime.now(UTC))
    assert "SPREAD_TOO_WIDE" in engine.assess(make_intent(session), 100, 0.2, with_quote(wide)).reason_codes
    assert "QUOTE_UNAVAILABLE" in engine.assess(make_intent(session), 100, 0.2, with_quote(None)).reason_codes
    failed = with_quote(fresh, provider_errors=["candle sync: HTTP 451"])
    assert "PROVIDER_FAILURE:candle sync: HTTP 451" in engine.assess(make_intent(session), 100, 0.2, failed).reason_codes
    future = MarketFacts("binance", "1h", NOW, NOW + timedelta(hours=1), quote=fresh, require_quote=True)
    assert "LOOKAHEAD_VIOLATION" in engine.assess(make_intent(session), 100, 0.2, future).reason_codes


def test_rejected_or_unvalidated_strategy_cannot_trade(session):
    from app.models import ValidationResult

    eligible(session, "BTCUSDT")
    intent = make_intent(session)
    version = session.get(StrategyVersion, intent.strategy_version_id)
    version.strategy.status = "retired"
    session.commit()
    assert "STRATEGY_NOT_PAPER_ELIGIBLE" in RiskEngine(session, Settings()).assess(intent, 100, 0.2, facts()).reason_codes
    other = make_intent(session)
    session.add(ValidationResult(strategy_version_id=other.strategy_version_id, method="robustness", result="FAIL", metrics={}))
    session.commit()
    assert "STRATEGY_NOT_PAPER_ELIGIBLE" in RiskEngine(session, Settings()).assess(other, 100, 0.2, facts()).reason_codes
