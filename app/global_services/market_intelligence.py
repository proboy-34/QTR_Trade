from datetime import datetime
from typing import Protocol


class MarketIntelligenceSource(Protocol):
    """Provider-neutral contract. V1 uses manual/API ingestion, not invented live news."""

    async def events(self, start: datetime, end: datetime) -> list[dict]: ...


HIGH_IMPACT_CATEGORIES = {"FOMC", "CPI", "PPI", "GDP", "NFP", "REGULATION", "EXCHANGE_INCIDENT"}


def is_high_impact(category: str, severity: str) -> bool:
    return category.upper() in HIGH_IMPACT_CATEGORIES or severity.upper() in {"HIGH", "CRITICAL"}

