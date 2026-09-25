"""Queryable research knowledge base (findings, failed ideas, lessons, patterns)."""

from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import KnowledgeEntry

KINDS = {
    "FINDING", "FAILED_IDEA", "STRATEGY_BEHAVIOR", "MARKET_PATTERN", "NEWS_RELATIONSHIP",
    "REGIME_OBSERVATION", "TRADE_LESSON", "COUNTERFACTUAL_FINDING",
}
SOURCE_TYPES = {"SYSTEM_DERIVED", "AI_INTERPRETATION", "OPERATOR"}


class KnowledgeBase:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, kind: str, title: str, *, body: str = "", evidence: dict[str, Any] | None = None,
        refs: dict[str, Any] | None = None, tags: list[str] | None = None, symbol: str | None = None,
        timeframe: str | None = None, regime: str | None = None, source_type: str = "SYSTEM_DERIVED",
        dedup_key: str | None = None,
    ) -> KnowledgeEntry:
        if kind not in KINDS or source_type not in SOURCE_TYPES:
            raise ValueError(f"Unsupported knowledge kind/source: {kind}/{source_type}")
        if dedup_key:
            existing = self.session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.dedup_key == dedup_key))
            if existing:
                # Repeated evidence strengthens an entry; earlier observations are retained.
                history = list((existing.evidence or {}).get("history", []))[-49:]
                history.append(evidence or {})
                existing.evidence = {**(evidence or {}), "history": history}
                existing.evidence_count = (existing.evidence_count or 1) + 1
                existing.body = body or existing.body
                self.session.flush()
                return existing
        entry = KnowledgeEntry(
            kind=kind, title=title[:300], body=body, evidence=evidence or {}, refs=refs or {},
            tags=tags or [], symbol=symbol, timeframe=timeframe, regime=regime,
            source_type=source_type, dedup_key=dedup_key,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def query(
        self, *, kind: str | None = None, symbol: str | None = None, regime: str | None = None,
        text: str | None = None, source_type: str | None = None, limit: int = 100,
    ) -> list[KnowledgeEntry]:
        query = select(KnowledgeEntry)
        if kind:
            query = query.where(KnowledgeEntry.kind == kind)
        if symbol:
            query = query.where(KnowledgeEntry.symbol == symbol)
        if regime:
            query = query.where(KnowledgeEntry.regime == regime)
        if source_type:
            query = query.where(KnowledgeEntry.source_type == source_type)
        if text:
            pattern = f"%{text.lower()}%"
            query = query.where(or_(func.lower(KnowledgeEntry.title).like(pattern), func.lower(KnowledgeEntry.body).like(pattern)))
        return list(self.session.scalars(query.order_by(desc(KnowledgeEntry.created_at)).limit(limit)).all())

    def counts(self) -> dict[str, int]:
        return dict(self.session.execute(
            select(KnowledgeEntry.kind, func.count()).group_by(KnowledgeEntry.kind)
        ).all())
