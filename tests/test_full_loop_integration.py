"""Deterministic end-to-end loop.

Market data -> universe -> scanner -> opportunity -> news -> AI research (fixture provider)
-> hypothesis -> backtest -> OOS -> walk-forward -> robustness -> candidate -> paper decisions
-> risk -> paper orders -> positions -> exits -> trade memory -> post-trade analysis
-> paper validation -> human review -> research memory -> new research candidate -> restart.

Only external services are replaced (Binance metadata, news source, Gemini). Every QTR layer
runs for real against the same database.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from market_fixtures import (
    EMA_SPEC,
    FakeUniverseProvider,
    candle_payload,
    instrument,
    liquid,
    oscillating,
    random_walk,
    store,
)
from paper_fixtures import FeedQuotes
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.ai.providers import AIRequest, AIResponse
from app.ai.research import AIResearchService
from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.global_services.live_paper import LivePaperService
from app.global_services.market_intelligence import IntelligenceService, RawIntelligenceItem
from app.global_services.scanner import MarketScanner
from app.global_services.strategy_repository import StrategyRepository
from app.global_services.universe import UniverseService
from app.learning.post_trade import PostTradeAnalyst
from app.learning.strategy_health import StrategyHealthService
from app.memory.trade_memory import reconstruct_decision
from app.models import (
    AIArtifact,
    Decision,
    DiscrepancyReport,
    Fill,
    Hypothesis,
    KnowledgeEntry,
    LivePaperSession,
    Opportunity,
    Order,
    PortfolioSnapshot,
    Position,
    PostTradeAnalysis,
    Strategy,
    SystemEvent,
    TradeMemory,
)
from app.research.generator import ResearchGenerator
from app.research.hypotheses import HypothesisEngine
from app.research.robustness import ValidationGates
from app.trading.reconciliation import ExchangeState
from app.trading.recovery import RecoveryService


class FixtureAI:
    name, model = "fixture-ai", "fixture-model"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    @property
    def configured(self) -> bool:
        return True

    async def generate(self, request: AIRequest) -> AIResponse:
        return AIResponse(json.dumps(self.payload), 800, 400, self.model)


@pytest.mark.asyncio
async def test_autonomous_research_to_paper_trade_to_learning_loop(session):
    settings = Settings(ai_trade_review="off")  # AI decisions are covered by their own tests
    bus = EventBus()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    # 1. Multi-asset market data (history before the live feed window).
    sol = oscillating(1740, end=now - timedelta(hours=2))
    history, feed = sol.iloc[:1600].copy(), sol.iloc[1600:]
    history.loc[history.index[-1], "volume"] = history["volume"].median() * 8  # unusual activity
    store(session, history, "SOLUSDT")
    store(session, oscillating(1600, seed=8, base=2500, end=history["timestamp"].iloc[-1]), "ETHUSDT")
    store(session, random_walk(1600, seed=5, end=history["timestamp"].iloc[-1]), "BTCUSDT")
    session.add(PortfolioSnapshot(equity=100_000, available_balance=100_000, exposure=0, margin_used=0, daily_pnl=0, drawdown=0))
    session.commit()

    # 2. Dynamic universe from exchange metadata (fixture for the public endpoints).
    provider = FakeUniverseProvider(
        [instrument("SOLUSDT", "SOL"), instrument("ETHUSDT", "ETH"), instrument("BTCUSDT", "BTC"), instrument("TINYUSDT", "TINY")],
        {"SOLUSDT": liquid("SOLUSDT"), "ETHUSDT": liquid("ETHUSDT", 2500, 1e9), "BTCUSDT": liquid("BTCUSDT", 60_000, 3e9),
         "TINYUSDT": liquid("TINYUSDT", volume=10)},
    )
    universe = await UniverseService(session, settings, provider).refresh()
    assert set(universe["eligible"]) == {"SOLUSDT", "ETHUSDT", "BTCUSDT"}

    # 3. Verified news for SOL, then 4. the scanner creates an opportunity (it never trades).
    news_time = feed["timestamp"].iloc[110].to_pydatetime()  # within the AI evidence window
    ingested = await IntelligenceService(session, bus).store([RawIntelligenceItem(
        provider="fixture_news", kind="news", external_id="sol-1", title="Solana network upgrade goes live",
        published_at=news_time, url="https://news.test/sol-1", currencies=["SOL"])])
    event_id = ingested["event_ids"][0]
    scan = await MarketScanner(session, settings, bus).scan("binance", "1h", UniverseService.eligible_symbols(session, "binance"))
    sol_result = next(item for item in scan["results"] if item["symbol"] == "SOLUSDT")
    assert sol_result["status"] == "OPPORTUNITY" and "VOLUME_SPIKE" in sol_result["signals"]
    assert session.scalar(select(func.count()).select_from(Order)) == 0

    # 5. AI research proposes a structured, testable hypothesis; claims pass the firewall.
    ai_payload = {
        "summary": "SOL shows unusual volume within an oscillating range.",
        "claims": [{"type": "FACT", "text": "A network upgrade went live", "refs": [f"event:{event_id}"]},
                   {"type": "FACT", "text": "Institutions are accumulating SOL", "refs": []},
                   {"type": "HYPOTHESIS", "text": "Trend reversals may be tradable", "refs": []}],
        "proposals": [{"statement": "EMA 5/15 crossovers capture SOL swings", "rationale": "range oscillation",
                       "spec": EMA_SPEC, "assumptions": ["oscillation persists"]}],
    }
    ai = AIResearchService(session, settings, bus, provider=FixtureAI(ai_payload))
    ai_result = await ai.generate_hypotheses("binance", "SOLUSDT", "1h")
    artifact = session.get(AIArtifact, ai_result["artifact_id"])
    assert [claim["status"] for claim in artifact.claims] == ["VERIFIED_BY_REFERENCE", "UNVERIFIED", "AI_HYPOTHESIS"]
    hypothesis = session.get(Hypothesis, ai_result["hypotheses_created"][0])

    # 6. Quantitative validation with the default (unloosened) research gates.
    engine = HypothesisEngine(session, settings, bus, gates=ValidationGates(min_paper_trades=2))
    await engine.run_until_paper(hypothesis)
    assert hypothesis.stage == "PAPER_TESTING", hypothesis.decision_reason
    candidate = session.get(Strategy, hypothesis.strategy_id)
    assert candidate.status == "paper_testing" and candidate.origin == "ai"

    # 7. Live-data paper trading on closed candles (public-data feed replayed deterministically).
    last_close: dict[str, float] = {}
    live = LivePaperService(factory, settings, bus, quote_provider=FeedQuotes(last_close.get))
    live.state.symbols, live.state.symbol, live.state.provider, live.state.status = ["SOLUSDT"], "SOLUSDT", "binance", "LIVE"
    outcomes = []
    for row in feed.itertuples():
        last_close["SOLUSDT"] = float(row.close)
        assert await live.ingest_closed_candle(candle_payload(row, "SOLUSDT"))
        outcomes.append(live.state.last_decision)
        closed = session.scalar(select(func.count()).select_from(Position).where(
            Position.status == "CLOSED", Position.strategy_version_id == candidate.versions[0].id))
        if closed >= 2:
            break
    session.expire_all()
    positions = session.scalars(select(Position).where(Position.strategy_version_id == candidate.versions[0].id)).all()
    assert "TRADE" in outcomes and len([p for p in positions if p.status == "CLOSED"]) >= 2
    fills = session.scalars(select(Fill)).all()
    assert fills and all((fill.price / Decimal("0.01")) % 1 == 0 for fill in fills)  # venue tick grid
    trades = session.scalars(select(TradeMemory).where(TradeMemory.strategy_version_id == candidate.versions[0].id)).all()
    assert len(trades) >= 2 and all(trade.decision_id and trade.lineage["code_version"] for trade in trades)
    assert all(trade.slippage_cost > 0 for trade in trades)  # paper fills are not perfect
    assert {trade.exit_reason for trade in trades} <= {"take_profit", "stop_loss", "strategy_exit"}
    assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "TRADE_CLOSED")) >= 2

    # 8. Post-trade learning and memory.
    for trade in trades:
        PostTradeAnalyst(session).analyze(trade)
    session.commit()
    assert session.scalar(select(func.count()).select_from(PostTradeAnalysis)) == len(trades)
    lessons = session.scalars(select(KnowledgeEntry).where(KnowledgeEntry.kind == "TRADE_LESSON")).all()
    assert len(lessons) == len(trades)
    trade_decision = session.get(Decision, trades[-1].decision_id)
    memory = reconstruct_decision(session, trade_decision.id)
    assert memory["strategy_versions"][0]["id"] == candidate.versions[0].id and memory["trade_memory"]
    health = await StrategyHealthService(session, settings, bus).evaluate(candidate)
    assert health["status"] == "INSUFFICIENT_DATA"

    # 9. Paper validation -> candidate for human review. Research cannot activate it.
    await engine.advance(hypothesis)
    assert hypothesis.stage == "PAPER_VALIDATED", hypothesis.evidence.get("paper_validation")
    assert session.scalar(select(func.count()).select_from(DiscrepancyReport)) == 1
    await engine.advance(hypothesis)
    session.refresh(candidate)
    assert hypothesis.stage == "CANDIDATE" and candidate.status == "ready_for_review"
    repository = StrategyRepository(session)
    with pytest.raises(SafetyError):
        repository.transition(candidate.id, "approved", "self-promotion attempt", actor="research")
    repository.transition(candidate.id, "approved", "reviewed by operator")
    repository.transition(candidate.id, "active", "operator promotion")
    session.refresh(hypothesis)
    assert hypothesis.stage == "ACTIVE"

    # 10. Accumulated evidence generates new research (not new trades).
    new_research = ResearchGenerator(session, settings, bus).from_opportunities()
    assert new_research and session.get(Hypothesis, new_research[0]).origin == "scanner"
    assert session.scalar(select(Opportunity).where(Opportunity.source == "scanner")).symbol == "SOLUSDT"

    # 11. Restart: persisted sessions are marked interrupted; state survives; recovery places no orders.
    session.add(LivePaperSession(provider="binance", symbol="SOLUSDT", timeframe="1h", status="LIVE"))
    session.commit()
    orders_before = session.scalar(select(func.count()).select_from(Order))
    restarted = LivePaperService(factory, settings, EventBus())
    assert restarted.recover_interrupted() == 1
    recovery = RecoveryService().recover(session, "paper", ExchangeState(orders={}, positions={}))
    assert recovery["corrective_orders"] == 0 and session.scalar(select(func.count()).select_from(Order)) == orders_before
    with factory() as fresh:
        assert fresh.scalar(select(func.count()).select_from(TradeMemory)) == len(trades)
