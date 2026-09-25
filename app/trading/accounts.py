"""Venue-scoped account state. Paper, testnet and live records are never mixed."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import PortfolioSnapshot

VENUES = ("paper", "testnet")


def venue_for(settings: Settings) -> str:
    """The execution venue for new orders. live_disabled has no venue (no orders at all)."""
    return {"paper": "paper", "testnet": "testnet"}.get(settings.execution_mode, "none")


def latest_portfolio(session: Session, venue: str = "paper") -> PortfolioSnapshot | None:
    return session.scalar(
        select(PortfolioSnapshot).where(PortfolioSnapshot.venue == venue).order_by(PortfolioSnapshot.captured_at.desc())
    )
