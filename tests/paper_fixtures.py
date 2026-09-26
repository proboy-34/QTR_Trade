"""DETERMINISTIC FIXTURES for paper-trading tests — never real market data or real trades."""

from datetime import datetime
from decimal import Decimal

from app.core.time import TimeService
from app.global_services.quotes import Quote, QuoteUnavailable
from app.models import ValidationResult
from app.trading.eligibility import REQUIRED_METHODS

# Legacy (non-declarative) strategies only decide in DEMO_MODE; AI review is tested separately.
LEGACY = {"demo_mode": True, "ai_trade_review": "off"}


def add_passes(session, version_id: str, **metrics) -> None:
    """Record PASS evidence for every required research method (fixture, not a real validation)."""
    for method in REQUIRED_METHODS:
        session.add(ValidationResult(strategy_version_id=version_id, method=method, result="PASS",
                                     metrics={"expectancy_pct": 1.0, "profit_factor": 2.0, "win_rate": 60, **metrics},
                                     configuration={"costs": {"slippage_rate": 0.0002, "spread_bps": 2}, "fixture": True},
                                     notes="deterministic test fixture"))
    session.flush()


class FixedQuotes:
    """Deterministic quote provider (fixture). Records how often it was asked."""

    def __init__(self, bid: float, ask: float, at: datetime | None = None, fail: bool = False) -> None:
        self.bid, self.ask, self.at, self.fail, self.calls = Decimal(str(bid)), Decimal(str(ask)), at, fail, 0

    async def quote(self, symbol: str) -> Quote:
        self.calls += 1
        if self.fail:
            raise QuoteUnavailable(f"fixture: no quote for {symbol}")
        return Quote(symbol, self.bid, self.ask, self.at or TimeService.now(), source="FIXTURE")


def validated_version(session, symbol: str = "BTCUSDT", status: str = "paper_testing", timeframe: str = "1h",
                      payload: dict | None = None):
    """A strategy version carrying fixture PASS evidence for every required method."""
    import hashlib
    from uuid import uuid4

    from app.models import Strategy, StrategyVersion

    name = f"fixture-{uuid4().hex[:8]}"
    strategy = Strategy(name=name, symbol=symbol, timeframe=timeframe, status=status)
    session.add(strategy)
    session.flush()
    fields = payload or {"parameters": {}, "entry_rules": [], "exit_rules": [], "filters": {}, "risk_assumptions": {},
                         "documentation": "fixture"}
    version = StrategyVersion(strategy_id=strategy.id, version=1, content_hash=hashlib.sha256(name.encode()).hexdigest(), **fields)
    session.add(version)
    session.flush()
    add_passes(session, version.id)
    return version



class FeedQuotes:
    """Fixture quotes around a price supplied by the test (e.g. the replayed candle close). Not real data."""

    def __init__(self, price_of, half_spread: float = 0.0001) -> None:
        self.price_of, self.half = price_of, Decimal(str(half_spread))

    async def quote(self, symbol: str) -> Quote:
        close = self.price_of(symbol)
        if close is None:
            raise QuoteUnavailable(f"fixture: no price for {symbol}")
        close = Decimal(str(close))
        return Quote(symbol, close * (1 - self.half), close * (1 + self.half), TimeService.now(), source="FIXTURE")
