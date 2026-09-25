import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.ai.firewall import ClaimVerifier, EvidenceRegistry, MalformedAIResponse, parse_json
from app.ai.gateway import AIBudgetExceeded, AIGateway, AIUnavailable
from app.ai.providers import (
    AIProviderError,
    AIRequest,
    AIResponse,
    GeminiProvider,
    UnconfiguredProvider,
)
from app.ai.research import AIResearchService
from app.core.config import Settings
from app.core.events import EventBus
from app.global_services.market_intelligence import (
    EventNormalizer,
    FinnhubMacroProvider,
    FinnhubNewsProvider,
    IntelligenceService,
    RawIntelligenceItem,
    configured_providers,
    is_high_impact,
)
from app.models import AIArtifact, AICallRecord, Hypothesis, MarketEvent, SystemEvent

NOW = datetime.now(UTC).replace(microsecond=0)


def test_provider_payload_parsing_keeps_source_identity():
    news = FinnhubNewsProvider.parse([{"id": 5, "headline": "SEC sues exchange", "datetime": 1_758_000_000,
                                       "url": "https://f.test/5", "source": "Reuters", "related": "BTC,COIN"}])
    assert news[0].source_name == "Reuters" and news[0].published_at.tzinfo is not None
    macro = FinnhubMacroProvider.parse({"economicCalendar": [
        {"event": "CPI MoM", "country": "US", "time": "2026-09-10 12:30:00", "estimate": 0.3, "actual": 0.5, "prev": 0.2,
         "impact": "high", "unit": "%"}]})
    assert macro[0].expected == "0.3" and macro[0].actual == "0.5" and macro[0].kind == "macro"


def test_normalization_structures_events_instead_of_sentiment():
    normalizer = EventNormalizer({"BTC", "ETH", "SOL"})
    hack = normalizer.normalize(RawIntelligenceItem("fixture", "news", "1", "Protocol exploit drains $50M on SOL chain",
                                                    NOW, url="https://x.test/1"))
    assert hack["event_type"] == "HACK" and hack["severity"] == "HIGH" and "SOL" in hack["affected_assets"]
    assert hack["verification_status"] == "SOURCE_VERIFIED" and hack["raw_reference"]["external_id"] == "1"
    cpi = normalizer.normalize(RawIntelligenceItem("fixture", "macro", "cpi", "CPI YoY", NOW - timedelta(hours=1),
                                                   country="US", expected="3.0%", actual="3.3%", impact="high"))
    assert cpi["event_type"] == "MACRO_CPI" and cpi["surprise"] == pytest.approx(0.3)
    assert {"BTC", "ETH", "RISK_ASSETS"} <= set(cpi["affected_assets"]) and cpi["status"] == "released"
    unsourced = normalizer.normalize(RawIntelligenceItem("fixture", "news", "2", "Rumour about ETH", NOW))
    assert unsourced["verification_status"] == "UNVERIFIED" and unsourced["event_type"] == "OTHER"
    assert is_high_impact("NEWS", "LOW", "HACK") and not is_high_impact("NEWS", "LOW", "OTHER")


@pytest.mark.asyncio
async def test_ingestion_deduplicates_and_updates_macro_actuals(session):
    service = IntelligenceService(session, EventBus())
    item = RawIntelligenceItem("fixture", "news", "abc", "Exchange delists token XYZ", NOW, url="https://x.test/abc")
    first = await service.store([item, item])
    assert first["created"] == 1 and first["duplicates"] == 1
    scheduled = RawIntelligenceItem("fixture_macro", "macro", "US:CPI", "CPI", NOW + timedelta(hours=2), country="US", expected="3.0")
    await service.store([scheduled])
    released = RawIntelligenceItem("fixture_macro", "macro", "US:CPI", "CPI", NOW + timedelta(hours=2), country="US",
                                   expected="3.0", actual="3.4")
    updated = await service.store([released])
    assert updated["updated"] == 1
    event = session.scalar(select(MarketEvent).where(MarketEvent.provider == "fixture_macro"))
    assert event.actual_value == "3.4" and event.surprise == pytest.approx(0.4)
    assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.type == "NEWS_EVENT")) == 2


def test_no_provider_configured_without_credentials():
    assert configured_providers(Settings(finnhub_api_key="")) == []
    assert len(configured_providers(Settings(finnhub_api_key="k"))) == 2
    assert len(configured_providers(Settings(finnhub_api_key="k", finnhub_calendar_enabled=False))) == 1


class FakeProvider:
    name, model = "fake", "fake-model-1"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    @property
    def configured(self) -> bool:
        return True

    async def generate(self, request: AIRequest) -> AIResponse:
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return AIResponse(item, 1000, 500, self.model)


async def no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_gateway_records_unconfigured_provider(session):
    gateway = AIGateway(session, Settings(), UnconfiguredProvider())
    with pytest.raises(AIUnavailable):
        await gateway.run(AIRequest("MARKET_CONTEXT_SUMMARY", "s", "p"))
    assert session.scalar(select(AICallRecord)).status == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_gateway_retries_retryable_failures_and_tracks_cost(session):
    provider = FakeProvider([AIProviderError("503", retryable=True), '{"summary": "ok", "claims": []}'])
    gateway = AIGateway(session, Settings(), provider, sleep=no_sleep)
    result = await gateway.run(AIRequest("T", "s", "p"))
    assert result.call.status == "SUCCEEDED" and result.call.attempts == 2
    assert result.call.input_tokens == 1000 and float(result.call.estimated_cost) == pytest.approx(0.0003 + 0.00125)


@pytest.mark.asyncio
async def test_gateway_fails_fast_on_non_retryable_and_times_out(session):
    gateway = AIGateway(session, Settings(), FakeProvider([AIProviderError("400 bad", retryable=False)]), sleep=no_sleep)
    with pytest.raises(AIUnavailable):
        await gateway.run(AIRequest("T", "s", "p"))

    class Slow(FakeProvider):
        async def generate(self, request):
            import asyncio
            await asyncio.sleep(1)

    slow = AIGateway(session, Settings(ai_timeout_seconds=0.01, ai_max_retries=1), Slow([]), sleep=no_sleep)
    with pytest.raises(AIUnavailable, match="timed out"):
        await slow.run(AIRequest("T", "s", "p"))
    records = session.scalars(select(AICallRecord).order_by(AICallRecord.created_at)).all()
    assert [record.status for record in records] == ["FAILED", "FAILED"] and records[-1].attempts == 2


@pytest.mark.asyncio
async def test_gateway_enforces_budget_and_hourly_limit(session):
    gateway = AIGateway(session, Settings(ai_daily_budget=0.0001), FakeProvider(['{}']))
    with pytest.raises(AIBudgetExceeded):
        await gateway.run(AIRequest("T", "s", "p" * 4000))
    limited = AIGateway(session, Settings(ai_max_requests_per_hour=1), FakeProvider(['{}', '{}']))
    await limited.run(AIRequest("T", "s", "p"))
    with pytest.raises(AIBudgetExceeded, match="Hourly"):
        await limited.run(AIRequest("T", "s", "p"))
    statuses = {record.status for record in session.scalars(select(AICallRecord)).all()}
    assert {"BLOCKED_BUDGET", "BLOCKED_RATE", "SUCCEEDED"} <= statuses


def test_firewall_never_lets_ai_create_facts():
    registry = EvidenceRegistry()
    registry.add("event:v", "SOURCE_FACT", {"title": "verified"})
    registry.add("event:u", "UNVERIFIED_SOURCE", {"title": "rumour"})
    registry.add("metric:rsi", "SYSTEM_METRIC", {"value": 61.5}, 61.5)
    claims, stats = ClaimVerifier(registry).verify([
        {"type": "FACT", "text": "Verified event happened", "refs": ["event:v"]},
        {"type": "FACT", "text": "BTC ETF inflows increased today", "refs": []},
        {"type": "FACT", "text": "Rumour", "refs": ["event:u"]},
        {"type": "FACT", "text": "Invented", "refs": ["event:made-up"]},
        {"type": "METRIC", "text": "RSI is 61.5", "refs": ["metric:rsi"]},
        {"type": "METRIC", "text": "RSI is 80", "refs": ["metric:rsi"]},
        {"type": "OPINION", "text": "Looks bullish", "refs": []},
        {"type": "HYPOTHESIS", "text": "Breakouts may continue", "refs": []},
        "not a claim",
    ])
    assert [claim["status"] for claim in claims] == [
        "VERIFIED_BY_REFERENCE", "UNVERIFIED", "UNVERIFIED", "UNVERIFIED", "SYSTEM_METRIC_MATCH", "UNVERIFIED",
        "AI_INTERPRETATION", "AI_HYPOTHESIS"]
    assert stats["facts_verified"] == 1 and stats["invalid"] == 1
    with pytest.raises(MalformedAIResponse):
        parse_json("not json")
    with pytest.raises(MalformedAIResponse):
        parse_json('{"claims": "x"}')
    assert parse_json('```json\n{"summary": "x"}\n```')["summary"] == "x"


@pytest.mark.asyncio
async def test_ai_hypotheses_become_research_artifacts_and_malicious_specs_are_rejected(session):
    from market_fixtures import EMA_SPEC, oscillating, store

    store(session, oscillating(300), "SOLUSDT")
    event = MarketEvent(category="NEWS", event_type="PROTOCOL_UPGRADE", title="Solana upgrade", severity="MEDIUM",
                        event_at=NOW - timedelta(hours=3), source="fixture", affected_assets=["SOL"], description="d",
                        verification_status="SOURCE_VERIFIED", provider="fixture")
    session.add(event)
    session.commit()
    payload = {
        "summary": "Context summary",
        "claims": [{"type": "FACT", "text": "An upgrade was announced", "refs": [f"event:{event.id}"]},
                   {"type": "FACT", "text": "Whales are buying", "refs": []}],
        "proposals": [
            {"statement": "EMA crossover captures SOL swings", "rationale": "oscillation", "spec": EMA_SPEC},
            {"statement": "Execute code", "spec": {**EMA_SPEC, "entry": [{"left": "__import__('os').system('x')", "operator": "gt", "right": 1}]}},
        ],
    }
    provider = FakeProvider([json.dumps(payload)])
    service = AIResearchService(session, Settings(), EventBus(), provider=provider)
    result = await service.generate_hypotheses("binance", "SOLUSDT", "1h")
    assert result["status"] == "OK" and len(result["hypotheses_created"]) == 1 and len(result["proposals_rejected"]) == 1
    artifact = session.get(AIArtifact, result["artifact_id"])
    assert [claim["status"] for claim in artifact.claims] == ["VERIFIED_BY_REFERENCE", "UNVERIFIED"]
    created = session.get(Hypothesis, result["hypotheses_created"][0])
    assert created.origin == "ai" and created.stage == "IDEA" and created.ai_artifact_id == artifact.id
    rejected = session.get(Hypothesis, result["proposals_rejected"][0])
    assert rejected.stage == "REJECTED" and rejected.decision_reason == "INVALID_SPEC"
    assert session.scalar(select(func.count()).select_from(MarketEvent)) == 1  # AI created no market facts
    cached = await service.generate_hypotheses("binance", "SOLUSDT", "1h")
    assert cached["status"] == "CACHED" and provider.calls == 1


@pytest.mark.asyncio
async def test_malformed_ai_response_is_recorded_not_trusted(session):
    service = AIResearchService(session, Settings(), EventBus(), provider=FakeProvider(["I think BTC goes up"]))
    result = await service.market_context("binance", "BTCUSDT", "1h")
    assert result["status"] == "MALFORMED_RESPONSE"
    assert session.scalar(select(AICallRecord)).status == "MALFORMED_RESPONSE"
    assert session.scalar(select(func.count()).select_from(AIArtifact)) == 0


@pytest.mark.asyncio
async def test_gemini_adapter_request_shape_and_secret_safety():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        if seen["body"]["contents"][0]["parts"][0]["text"] == "fail":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"summary": "x"}'}]}, "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5}, "modelVersion": "gemini-test"})

    provider = GeminiProvider("secret-key-123", "gemini-2.5-flash", "https://gl.test/v1beta", transport=httpx.MockTransport(handler))
    response = await provider.generate(AIRequest("T", "system", "hello"))
    assert response.text == '{"summary": "x"}' and response.input_tokens == 12 and response.model == "gemini-test"
    assert "secret-key-123" not in seen["url"] and seen["key"] == "secret-key-123"
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    with pytest.raises(AIProviderError) as error:
        await provider.generate(AIRequest("T", "system", "fail"))
    assert error.value.retryable and "secret-key-123" not in str(error.value)


@pytest.mark.asyncio
async def test_ai_trade_review_can_only_support_reject_or_fail_honestly(session):
    rationale = {"asset": "SOLUSDT", "decision_time": NOW.isoformat(), "direction": "LONG",
                 "supporting_evidence": [{"code": "SETUP", "detail": "EMA cross", "source": "strategy_rule"}],
                 "contradicting_evidence": [{"code": "BTC_CONTEXT", "detail": "BTC trending down", "source": "regime_engine"}],
                 "macro_context": {}, "news_context": []}
    reject = {"summary": "s", "claims": [], "proposals": [{"verdict": "REJECT", "reasons": ["BTC trending down"], "size": 99}]}
    review = await AIResearchService(session, Settings(), EventBus(), provider=FakeProvider([json.dumps(reject)])).review_trade(rationale)
    assert review["verdict"] == "REJECT" and "size" not in review  # the AI cannot re-size or re-price
    odd = {"summary": "s", "claims": [], "proposals": [{"verdict": "BUY MORE"}]}
    rationale["decision_time"] = (NOW + timedelta(minutes=1)).isoformat()
    review = await AIResearchService(session, Settings(), EventBus(), provider=FakeProvider([json.dumps(odd)])).review_trade(rationale)
    assert review["verdict"] == "UNCERTAIN"
    rationale["decision_time"] = (NOW + timedelta(minutes=2)).isoformat()
    down = FakeProvider([AIProviderError("400 bad", retryable=False)])
    review = await AIResearchService(session, Settings(), EventBus(), provider=down).review_trade(rationale)
    assert review["status"] == "UNAVAILABLE" and review["verdict"] is None  # no fabricated opinion
