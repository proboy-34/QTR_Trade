"""Provider-neutral market intelligence: news, macro calendars, exchange/regulatory events.

Every stored event keeps its provider, external identity, source and timestamps.
Items without a traceable source are stored as UNVERIFIED. AI output never enters here.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.events import Event, EventBus, publish_persisted
from app.core.time import TimeService
from app.models import Asset, MarketEvent

HIGH_IMPACT_CATEGORIES = {"FOMC", "CPI", "PPI", "GDP", "NFP", "REGULATION", "EXCHANGE_INCIDENT"}
HIGH_IMPACT_TYPES = {"HACK", "EXCHANGE_INCIDENT", "DELISTING", "REGULATION", "MACRO_CPI", "MACRO_FOMC", "MACRO_NFP"}


def is_high_impact(category: str, severity: str, event_type: str = "") -> bool:
    return (
        category.upper() in HIGH_IMPACT_CATEGORIES
        or severity.upper() in {"HIGH", "CRITICAL"}
        or event_type.upper() in HIGH_IMPACT_TYPES
    )


@dataclass
class RawIntelligenceItem:
    provider: str
    kind: str  # news | macro
    external_id: str
    title: str
    published_at: datetime
    summary: str = ""
    url: str | None = None
    source_name: str = ""
    currencies: list[str] = field(default_factory=list)
    country: str | None = None
    expected: str | None = None
    actual: str | None = None
    previous: str | None = None
    unit: str | None = None
    impact: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MarketIntelligenceProvider(Protocol):
    name: str
    kind: str

    async def fetch(self, since: datetime) -> list[RawIntelligenceItem]: ...


# Kept for backwards compatibility with the V1 contract name.
MarketIntelligenceSource = MarketIntelligenceProvider


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    return TimeService.ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


class CryptoPanicProvider:
    """CryptoPanic developer API. Requires NEWS_PROVIDER_API_KEY (auth_token)."""

    name, kind = "cryptopanic", "news"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://cryptopanic.com/api/developer/v2"

    @staticmethod
    def parse(payload: dict[str, Any]) -> list[RawIntelligenceItem]:
        items: list[RawIntelligenceItem] = []
        for post in payload.get("results", []):
            if not post.get("id") or not post.get("title") or not post.get("published_at"):
                continue
            instruments = post.get("instruments") or post.get("currencies") or []
            source = post.get("source") or {}
            items.append(RawIntelligenceItem(
                provider="cryptopanic", kind="news", external_id=str(post["id"]), title=post["title"],
                published_at=_timestamp(post["published_at"]), summary=post.get("description") or "",
                url=post.get("original_url") or post.get("url"),
                source_name=source.get("title") or source.get("domain") or "cryptopanic",
                currencies=[str(item.get("code", "")).upper() for item in instruments if item.get("code")],
                raw={"kind": post.get("kind"), "domain": source.get("domain"), "votes": post.get("votes")},
            ))
        return items

    async def fetch(self, since: datetime) -> list[RawIntelligenceItem]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.get("/posts/", params={"auth_token": self.api_key, "public": "true"})
            response.raise_for_status()
            return [item for item in self.parse(response.json()) if item.published_at >= since]


class FinnhubNewsProvider:
    """Finnhub market news (category=crypto). Requires NEWS_PROVIDER_API_KEY."""

    name, kind = "finnhub", "news"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://finnhub.io/api/v1"

    @staticmethod
    def parse(payload: list[dict[str, Any]]) -> list[RawIntelligenceItem]:
        items: list[RawIntelligenceItem] = []
        for item in payload:
            if not item.get("id") or not item.get("headline") or not item.get("datetime"):
                continue
            related = [part.strip().upper() for part in str(item.get("related") or "").split(",") if part.strip()]
            items.append(RawIntelligenceItem(
                provider="finnhub", kind="news", external_id=str(item["id"]), title=item["headline"],
                published_at=_timestamp(int(item["datetime"])), summary=item.get("summary") or "",
                url=item.get("url"), source_name=item.get("source") or "finnhub", currencies=related,
                raw={"category": item.get("category")},
            ))
        return items

    async def fetch(self, since: datetime) -> list[RawIntelligenceItem]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.get("/news", params={"category": "crypto", "token": self.api_key})
            response.raise_for_status()
            return [item for item in self.parse(response.json()) if item.published_at >= since]


class FinnhubMacroProvider:
    """Finnhub economic calendar. Requires MACRO_PROVIDER_API_KEY."""

    name, kind = "finnhub_macro", "macro"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url or "https://finnhub.io/api/v1"

    @staticmethod
    def parse(payload: dict[str, Any]) -> list[RawIntelligenceItem]:
        items: list[RawIntelligenceItem] = []
        for item in payload.get("economicCalendar", []):
            if not item.get("event") or not item.get("time"):
                continue
            at = _timestamp(item["time"])
            identity = f"{item.get('country', '')}:{item['event']}:{at.isoformat()}"
            items.append(RawIntelligenceItem(
                provider="finnhub_macro", kind="macro", external_id=identity, title=item["event"],
                published_at=at, source_name="finnhub economic calendar", country=item.get("country"),
                expected=None if item.get("estimate") is None else str(item["estimate"]),
                actual=None if item.get("actual") is None else str(item["actual"]),
                previous=None if item.get("prev") is None else str(item["prev"]),
                unit=item.get("unit"), impact=item.get("impact"), raw={"impact": item.get("impact")},
            ))
        return items

    async def fetch(self, since: datetime) -> list[RawIntelligenceItem]:
        today = TimeService.now().date()
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20) as client:
            response = await client.get("/calendar/economic", params={
                "from": since.date().isoformat(), "to": (today + timedelta(days=7)).isoformat(), "token": self.api_key,
            })
            response.raise_for_status()
            return self.parse(response.json())


TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("HACK", ("hack", "exploit", "drained", "stolen", "breach")),
    ("EXCHANGE_INCIDENT", ("outage", "halts withdrawals", "suspends withdrawals", "insolv", "downtime")),
    ("DELISTING", ("delist",)),
    ("LISTING", ("will list", "lists ", "listing", "launchpool")),
    ("TOKEN_UNLOCK", ("unlock",)),
    ("ETF", (" etf", "etf ", "spot etf", "etf inflow", "etf outflow")),
    ("REGULATION", ("sec ", "regulat", "lawsuit", "cftc", "ban ", "sanction", "mica")),
    ("PROTOCOL_UPGRADE", ("upgrade", "hard fork", "mainnet", "halving")),
    ("NETWORK_EVENT", ("network congestion", "chain halt", "reorg")),
    ("FUNDING", ("funding rate", "liquidation")),
    ("INSTITUTIONAL", ("blackrock", "fidelity", "microstrategy", "treasury", "institutional")),
)
MACRO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("MACRO_CPI", ("cpi", "consumer price")),
    ("MACRO_PPI", ("ppi", "producer price")),
    ("MACRO_FOMC", ("fomc", "fed interest rate", "federal funds", "interest rate decision")),
    ("MACRO_NFP", ("nonfarm", "non-farm", "nfp")),
    ("MACRO_GDP", ("gdp",)),
    ("MACRO_UNEMPLOYMENT", ("unemployment",)),
)


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
    return float(match.group()) if match else None


class EventNormalizer:
    """Deterministic, reviewable normalization. Unknown content stays OTHER rather than guessed."""

    def __init__(self, known_assets: set[str]) -> None:
        self.known_assets = {item.upper() for item in known_assets}

    def normalize(self, item: RawIntelligenceItem) -> dict[str, Any]:
        text = f" {item.title} {item.summary} ".lower()
        rules = MACRO_RULES if item.kind == "macro" else TYPE_RULES
        event_type = next((name for name, words in rules if any(word in text for word in words)), "OTHER")
        if item.kind == "macro" and event_type == "OTHER":
            event_type = "MACRO_OTHER"
        assets = {code for code in item.currencies if code}
        for token in re.findall(r"\b[A-Z]{2,10}\b", f"{item.title} {item.summary}"):
            if token in self.known_assets:
                assets.add(token)
        if item.kind == "macro":
            if (item.country or "").upper() in {"US", "USA"} or event_type != "MACRO_OTHER":
                assets |= {"BTC", "ETH", "RISK_ASSETS"}
        expected, actual = _number(item.expected), _number(item.actual)
        surprise = round(actual - expected, 6) if expected is not None and actual is not None else None
        high = event_type in HIGH_IMPACT_TYPES or (item.impact or "").lower() == "high"
        severity = "HIGH" if high else ("MEDIUM" if event_type != "OTHER" else "LOW")
        relevance = 0.0
        if assets:
            relevance += 0.4
        if event_type not in {"OTHER", "MACRO_OTHER"}:
            relevance += 0.3
        if high:
            relevance += 0.3
        now = TimeService.now()
        status = "published"
        if item.kind == "macro":
            status = "released" if item.actual is not None else ("scheduled" if item.published_at > now else "pending_release")
        return {
            "category": "MACRO" if item.kind == "macro" else "NEWS",
            "event_type": event_type, "title": item.title[:300], "severity": severity,
            "event_at": item.published_at, "published_at": item.published_at,
            "source": item.source_name or item.provider, "source_url": item.url,
            "affected_assets": sorted(assets), "description": item.summary or item.title,
            "status": status, "provider": item.provider, "external_id": item.external_id,
            "verification_status": "SOURCE_VERIFIED" if (item.url or item.kind == "macro") and item.external_id else "UNVERIFIED",
            "relevance": round(min(1.0, relevance), 3), "expected_value": item.expected,
            "actual_value": item.actual, "previous_value": item.previous, "surprise": surprise,
            "unit": item.unit, "country": item.country,
            "raw_reference": {"provider": item.provider, "external_id": item.external_id, "url": item.url, **item.raw},
            "processing_status": "NORMALIZED",
        }


def dedup_hash(provider: str, external_id: str) -> str:
    return hashlib.sha256(f"{provider}:{external_id}".encode()).hexdigest()


def configured_providers(settings: Settings) -> list[MarketIntelligenceProvider]:
    providers: list[MarketIntelligenceProvider] = []
    if settings.news_provider == "cryptopanic" and settings.news_provider_api_key:
        providers.append(CryptoPanicProvider(settings.news_provider_api_key, settings.news_provider_base_url))
    if settings.news_provider == "finnhub" and settings.news_provider_api_key:
        providers.append(FinnhubNewsProvider(settings.news_provider_api_key, settings.news_provider_base_url))
    if settings.macro_provider == "finnhub" and settings.macro_provider_api_key:
        providers.append(FinnhubMacroProvider(settings.macro_provider_api_key))
    return providers


def provider_status(settings: Settings) -> dict[str, Any]:
    return {
        "news": {
            "provider": settings.news_provider or None,
            "configured": bool(settings.news_provider and settings.news_provider_api_key),
            "credential": "NEWS_PROVIDER_API_KEY",
        },
        "macro": {
            "provider": settings.macro_provider or None,
            "configured": bool(settings.macro_provider and settings.macro_provider_api_key),
            "credential": "MACRO_PROVIDER_API_KEY",
        },
        "manual": {"provider": "operator", "configured": True},
    }


class IntelligenceService:
    def __init__(self, session: Session, event_bus: EventBus) -> None:
        self.session = session
        self.event_bus = event_bus

    def normalizer(self) -> EventNormalizer:
        bases = set(self.session.scalars(select(Asset.base_asset)).all())
        return EventNormalizer(bases | {"BTC", "ETH", "SOL", "BNB", "XRP"})

    async def ingest(self, provider: MarketIntelligenceProvider, since: datetime | None = None) -> dict[str, Any]:
        since = since or TimeService.now() - timedelta(days=2)
        items = await provider.fetch(since)
        return await self.store(items)

    async def store(self, items: list[RawIntelligenceItem]) -> dict[str, Any]:
        normalizer = self.normalizer()
        created, duplicates, updated = [], 0, 0
        for item in items:
            key = dedup_hash(item.provider, item.external_id)
            existing = self.session.scalar(select(MarketEvent).where(MarketEvent.dedup_hash == key))
            data = normalizer.normalize(item)
            if existing:
                # Macro releases gain an actual value later; that update is new information.
                if existing.actual_value is None and data["actual_value"] is not None:
                    existing.actual_value = data["actual_value"]
                    existing.surprise = data["surprise"]
                    existing.status = data["status"]
                    updated += 1
                else:
                    duplicates += 1
                continue
            event = MarketEvent(dedup_hash=key, **data)
            self.session.add(event)
            self.session.flush()
            created.append(event)
            await publish_persisted(self.session, self.event_bus, Event("NEWS_EVENT", {
                "market_event_id": event.id, "event_type": event.event_type, "provider": event.provider,
                "affected_assets": event.affected_assets, "verification_status": event.verification_status,
            }, source="market_intelligence"), "market_intelligence")
            if is_high_impact(event.category, event.severity, event.event_type):
                await publish_persisted(self.session, self.event_bus, Event(
                    "HighImpactEventDetected", {"market_event_id": event.id, "message": event.title},
                ), "market_intelligence")
        self.session.commit()
        return {"received": len(items), "created": len(created), "duplicates": duplicates, "updated": updated,
                "event_ids": [item.id for item in created]}
