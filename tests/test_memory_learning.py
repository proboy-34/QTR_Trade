import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from market_fixtures import EMA_SPEC, oscillating, store
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.events import EventBus
from app.learning.counterfactual import CounterfactualService
from app.learning.paper_validation import PaperValidationService
from app.learning.post_trade import PostTradeAnalyst
from app.learning.strategy_health import StrategyHealthService
from app.memory.knowledge import KnowledgeBase
from app.memory.market_memory import MarketMemoryService, context_features
from app.memory.trade_memory import TradeMemoryService, reconstruct_decision
from app.models import (
    CounterfactualEvaluation,
    Decision,
    DiscrepancyReport,
    Hypothesis,
    MarketContextRecord,
    MarketEvent,
    Opportunity,
    PortfolioSnapshot,
    Position,
    Strategy,
    StrategyVersion,
    SystemEvent,
    TradeMemory,
    ValidationResult,
)
from app.research.dsl import parse_spec, version_payload
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.positions import PositionManager

NOW = datetime.now(UTC).replace(microsecond=0)


def strategy_with_version(session, status="active", oos_expectancy=1.0, name="Learner") -> tuple[Strategy, StrategyVersion]:
    strategy = Strategy(name=name, symbol="BTCUSDT", timeframe="1h", status=status)
    session.add(strategy)
    session.flush()
    payload = version_payload(parse_spec({**EMA_SPEC, "universe": ["BTCUSDT"]}), "learning test strategy")
    version = StrategyVersion(strategy_id=strategy.id, version=1, content_hash=hashlib.sha256(name.encode()).hexdigest(), **payload)
    session.add(version)
    session.flush()
    session.add(ValidationResult(strategy_version_id=version.id, method="out_of_sample", result="PASS",
                                 metrics={"expectancy_pct": oos_expectancy, "profit_factor": 2.0, "win_rate": 60},
                                 configuration={"costs": {"slippage_rate": 0.0002, "spread_bps": 2}}))
    session.add(PortfolioSnapshot(equity=100_000, available_balance=100_000, exposure=0, margin_used=0, daily_pnl=0, drawdown=0))
    session.commit()
    return strategy, version


def closed_trade(session, version: StrategyVersion, pnl_per_unit: float, index: int, regime: str = "TRENDING_UP") -> TradeMemory:
    opened = NOW - timedelta(hours=100 - index * 2)
    position = Position(strategy_version_id=version.id, symbol="BTCUSDT", side="BUY", quantity=1, entry_price=100,
                        current_price=100, stop_loss=96, take_profit=108, status="OPEN", opened_at=opened,
                        highest_price=Decimal("104"), lowest_price=Decimal("98"))
    session.add(position)
    session.commit()
    PositionManager(session, 0).close(position, 100 + pnl_per_unit, "take_profit" if pnl_per_unit > 0 else "stop_loss")
    position.closed_at = opened + timedelta(hours=1)
    trade = TradeMemoryService(session).record(position)
    trade.regime_at_entry = regime
    session.commit()
    return trade


def test_trade_memory_is_immutable_and_records_excursions(session):
    _, version = strategy_with_version(session)
    trade = closed_trade(session, version, 5, 0)
    assert trade.mfe_pct == pytest.approx(5.0) and trade.mae_pct == pytest.approx(-2.0)
    assert trade.net_pnl == Decimal("5.0000000000") and trade.r_multiple == pytest.approx(1.25)
    position = session.get(Position, trade.position_id)
    again = TradeMemoryService(session).record(position)
    assert again.id == trade.id and session.scalar(select(func.count()).select_from(TradeMemory)) == 1
    open_position = Position(strategy_version_id=version.id, symbol="BTCUSDT", side="BUY", quantity=1, entry_price=1,
                             current_price=1, stop_loss=0.5, take_profit=2, status="OPEN")
    session.add(open_position)
    session.commit()
    assert TradeMemoryService(session).record(open_position) is None


def test_post_trade_analysis_separates_expectation_from_outcome(session):
    _, version = strategy_with_version(session)
    winner = closed_trade(session, version, 8, 0)
    analysis = PostTradeAnalyst(session).analyze(winner)
    assert analysis.thesis_correct is True and analysis.expected["reward_to_risk"] == pytest.approx(2.0)
    assert analysis.expected["research_expectancy_pct"] == 1.0 and analysis.knowledge_entry_id
    loser = closed_trade(session, version, -4, 1)
    loser.mfe_pct = 0.5
    bad = PostTradeAnalyst(session).analyze(loser)
    assert bad.thesis_correct is False and bad.entry_quality == "POOR_IMMEDIATELY_ADVERSE"
    assert PostTradeAnalyst(session).analyze(winner).id == analysis.id
    kinds = KnowledgeBase(session).counts()
    assert kinds["TRADE_LESSON"] == 2 and kinds["STRATEGY_BEHAVIOR"] == 2


def test_counterfactuals_record_why_we_did_not_trade_and_evaluate_outcome(session):
    frame = oscillating(200, end=NOW - timedelta(hours=30))
    store(session, frame, "SOLUSDT")
    reference = frame.iloc[150]
    context = MarketContextRecord(symbol="SOLUSDT", exchange="binance", timeframe="1h", price=Decimal(str(round(reference.close, 8))),
                                  regime="trending", direction="BULLISH", volatility=0.1, liquidity="STRONG",
                                  observed_at=reference.timestamp.to_pydatetime(), snapshot={})
    session.add(context)
    session.flush()
    session.add(Opportunity(market_context_id=context.id, symbol="SOLUSDT", status="EXPIRED", source="scanner",
                            exchange="binance", timeframe="1h", reasons=["BREAKOUT_UP"]))
    session.commit()
    service = CounterfactualService(session, horizon_bars=12)
    assert service.capture() == 1 and service.capture() == 0
    result = service.evaluate_pending()
    item = session.scalar(select(CounterfactualEvaluation))
    assert result["evaluated"] == 1 and item.status == "EVALUATED"
    expected = (float(frame.iloc[162].close) / float(reference.close) - 1) * 100
    assert item.forward_return_pct == pytest.approx(expected, abs=1e-3)
    assert item.verdict in {"MISSED_OPPORTUNITY", "CORRECT_AVOIDANCE", "NEUTRAL"} and item.rejection_category == "NO_ELIGIBLE_STRATEGY"


@pytest.mark.asyncio
async def test_risk_rejected_signal_is_captured_as_counterfactual(session):
    strategy, _ = strategy_with_version(session)
    session.add(PortfolioSnapshot(equity=100_000, available_balance=100_000, exposure=0.5, margin_used=0, daily_pnl=0, drawdown=0))
    session.commit()
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(
        MarketSnapshot("BTCUSDT", 65_000, 2_000, .4, 0, "trending", "BULLISH"))
    assert result["risk_outcome"] == "REJECTED"
    CounterfactualService(session).capture()
    item = session.scalar(select(CounterfactualEvaluation))
    assert item.rejection_category == "RISK_REJECTED" and "MAX_EXPOSURE" in item.rejection_reasons


@pytest.mark.asyncio
async def test_degradation_is_flagged_without_modifying_strategy_and_generates_research(session):
    strategy, version = strategy_with_version(session, oos_expectancy=2.0)
    for index in range(6):
        closed_trade(session, version, 6, index, regime="TRENDING_UP")
    for index in range(6, 20):
        closed_trade(session, version, -4, index, regime="RANGING")
    status_before, hash_before = strategy.status, version.content_hash
    result = await StrategyHealthService(session, Settings(), EventBus()).evaluate(strategy)
    assert result["status"] == "DEGRADED" and strategy.status == status_before and version.content_hash == hash_before
    assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "STRATEGY_DEGRADED")) == 1
    follow_up = session.get(Hypothesis, result["follow_up_hypothesis_id"])
    assert follow_up.origin == "strategy_health" and follow_up.spec["regimes"] == ["TRENDING_UP"]
    again = await StrategyHealthService(session, Settings(), EventBus()).evaluate(strategy)
    assert again["follow_up_hypothesis_id"] is None  # no duplicate research or duplicate event


@pytest.mark.asyncio
async def test_healthy_and_insufficient_evidence_states(session):
    strategy, version = strategy_with_version(session, oos_expectancy=2.0)
    service = StrategyHealthService(session, Settings(), EventBus())
    assert (await service.evaluate(strategy))["status"] == "INSUFFICIENT_DATA"
    for index in range(10):
        closed_trade(session, version, 3 if index % 3 else -1, index)
    assert (await service.evaluate(strategy))["status"] == "HEALTHY"


def test_paper_validation_and_discrepancy_report(session):
    from app.research.robustness import ValidationGates

    strategy, version = strategy_with_version(session, status="paper_testing", oos_expectancy=3.0)
    service = PaperValidationService(session, ValidationGates(min_paper_trades=4))
    closed_trade(session, version, 5, 0)
    assert service.evaluate(strategy)["result"] == "INSUFFICIENT_DATA"
    for index in range(1, 5):
        closed_trade(session, version, 4 if index != 2 else -2, index)
    verdict = service.evaluate(strategy)
    assert verdict["result"] == "PASS" and verdict["checks"]["consistent_with_backtest"]
    report = session.get(DiscrepancyReport, verdict["discrepancy_report_id"])
    assert report.backtest["expectancy_pct"] == 3.0 and "expectancy_pct" in report.differences
    stored = session.scalar(select(ValidationResult).where(ValidationResult.method == "paper_validation"))
    assert stored.result == "PASS"


def test_market_memory_similarity_returns_evidence_not_prediction(session):
    frame = oscillating(400)
    store(session, frame, "SOLUSDT")
    memory = MarketMemoryService(session)
    for end in range(100, 390, 10):
        window = frame.iloc[:end]
        memory.remember(exchange="binance", symbol="SOLUSDT", timeframe="1h", observed_at=window["timestamp"].iloc[-1],
                        price=window["close"].iloc[-1], features=context_features(window), regime="RANGING",
                        regime_confidence=0.6, legacy_regime="sideways", direction="BULLISH")
    duplicate = memory.remember(exchange="binance", symbol="SOLUSDT", timeframe="1h", observed_at=frame.iloc[:100]["timestamp"].iloc[-1],
                                price=1, features={}, regime="X", regime_confidence=0, legacy_regime="x", direction="x")
    assert duplicate.market_regime == "RANGING"  # idempotent per candle
    result = memory.similar(context_features(frame), timeframe="1h", limit=5)
    assert len(result["matches"]) == 5 and result["summary"]["with_known_outcome"] >= 1
    assert "not a prediction" in result["note"]
    assert memory.similar({}, timeframe="1h")["matches"] == []


@pytest.mark.asyncio
async def test_decision_memory_reconstructs_inputs_by_reference(session):
    strategy, version = strategy_with_version(session)
    event = MarketEvent(category="NEWS", event_type="ETF", title="BTC ETF approval", severity="MEDIUM", event_at=NOW - timedelta(hours=1),
                        source="fixture", affected_assets=["BTC"], description="d", verification_status="SOURCE_VERIFIED")
    session.add(event)
    session.commit()
    result = await TradingPipeline(session, Settings(), EventBus()).evaluate(
        MarketSnapshot("BTCUSDT", 65_000, 2_000, .3, 0, "trending", "BULLISH"))
    decision = session.get(Decision, result["decision_id"])
    lineage = decision.lineage
    assert lineage["code_version"] and lineage["risk_configuration"]["max_total_exposure"] == 0.5
    assert lineage["strategy_versions"][0]["id"] == version.id and event.id in lineage["event_ids"]
    memory = reconstruct_decision(session, decision.id)
    assert memory["strategy_versions"][0]["content_hash"] == version.content_hash
    assert memory["news_available"][0]["title"] == "BTC ETF approval"
    assert memory["positions"] and memory["orders"] and memory["risk_assessments"][0]["outcome"] == "APPROVED"
    assert reconstruct_decision(session, "missing") is None


def test_knowledge_base_query_and_evidence_accumulation(session):
    base = KnowledgeBase(session)
    base.record("MARKET_PATTERN", "Breakouts fail in low volatility", body="evidence", symbol="SOLUSDT", regime="LOW_VOLATILITY",
                dedup_key="pattern:1", evidence={"n": 5})
    entry = base.record("MARKET_PATTERN", "Breakouts fail in low volatility", dedup_key="pattern:1", evidence={"n": 9})
    assert entry.evidence_count == 2 and len(entry.evidence["history"]) == 1
    assert base.query(text="breakouts")[0].id == entry.id and base.query(kind="TRADE_LESSON") == []
    with pytest.raises(ValueError):
        base.record("NOT_A_KIND", "x")
