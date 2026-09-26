from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.core.config import get_settings


class PrivateExecutionAdapter(Protocol):
    """Future authenticated boundary. No live implementation is registered in V1."""

    async def account_state(self) -> dict: ...
    async def submit_order(self, execution_plan: dict, client_order_id: str) -> dict: ...
    async def cancel_order(self, exchange_order_id: str) -> dict: ...
    async def open_orders(self) -> list[dict]: ...
    async def open_positions(self) -> list[dict]: ...


class ExchangeAdapter(ABC):
    """Normalizes exchange-specific public market data into QTR records."""

    name: str
    base_url: str

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    @abstractmethod
    async def ticker(self, symbol: str) -> dict: ...

    async def ping(self) -> bool:
        await self.ticker("BTCUSDT")
        return True


class BinanceAdapter(ExchangeAdapter):
    name, base_url = "binance", "https://api.binance.com"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = base_url or self.base_url

    async def ticker(self, symbol: str) -> dict:
        data = await self._get("/api/v3/ticker/24hr", {"symbol": symbol})
        return {
            "exchange": self.name,
            "symbol": symbol,
            "price": float(data["lastPrice"]),
            "volume": float(data["volume"]),
            "timestamp": datetime.fromtimestamp(data["closeTime"] / 1000, UTC).isoformat(),
        }


class OKXAdapter(ExchangeAdapter):
    name, base_url = "okx", "https://www.okx.com"

    async def ticker(self, symbol: str) -> dict:
        instrument = symbol.replace("USDT", "-USDT")
        data = (await self._get("/api/v5/market/ticker", {"instId": instrument}))["data"][0]
        return {
            "exchange": self.name,
            "symbol": symbol,
            "price": float(data["last"]),
            "volume": float(data["vol24h"]),
            "timestamp": datetime.fromtimestamp(int(data["ts"]) / 1000, UTC).isoformat(),
        }


class BybitAdapter(ExchangeAdapter):
    name, base_url = "bybit", "https://api.bybit.com"

    async def ticker(self, symbol: str) -> dict:
        data = (await self._get("/v5/market/tickers", {"category": "spot", "symbol": symbol}))["result"]["list"][0]
        return {
            "exchange": self.name,
            "symbol": symbol,
            "price": float(data["lastPrice"]),
            "volume": float(data["volume24h"]),
            "timestamp": datetime.now(UTC).isoformat(),
        }


EXCHANGES: dict[str, ExchangeAdapter] = {
    "binance": BinanceAdapter(get_settings().binance_public_base_url),
    "okx": OKXAdapter(),
    "bybit": BybitAdapter(),
}
