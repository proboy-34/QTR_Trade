"""Real top-of-book quotes from Binance public REST (no credentials).

Paper fills are simulated against these quotes: a paper BUY pays the real ask, a paper SELL
receives the real bid, and configured slippage is applied on top. A quote is never invented;
if it cannot be fetched or is malformed, callers must not trade.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

import httpx

from app.core.decimal_math import ZERO, decimal
from app.core.time import TimeService


class QuoteUnavailable(ConnectionError):
    pass


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    source: str = "REAL_BINANCE"

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 10_000 if self.mid > ZERO else Decimal("Infinity")


class QuoteProvider(Protocol):
    async def quote(self, symbol: str) -> Quote: ...


class BinanceQuoteProvider:
    def __init__(self, base_url: str, timeout: float = 10, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url, self.timeout, self.transport = base_url, timeout, transport

    async def quote(self, symbol: str) -> Quote:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as client:
                response = await client.get("/api/v3/ticker/bookTicker", params={"symbol": symbol})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QuoteUnavailable(f"Binance bookTicker {symbol} failed: {type(exc).__name__}: {exc}") from None
        try:
            bid, ask = decimal(payload["bidPrice"]), decimal(payload["askPrice"])
        except (KeyError, TypeError, ArithmeticError, ValueError):
            raise QuoteUnavailable(f"Binance bookTicker {symbol}: malformed payload") from None
        if bid <= ZERO or ask <= ZERO or ask < bid or payload.get("symbol", symbol) != symbol:
            raise QuoteUnavailable(f"Binance bookTicker {symbol}: invalid bid/ask {bid}/{ask}")
        return Quote(symbol, bid, ask, TimeService.now())
