"""Dynamic multi-asset universe and eligibility engine.

Instrument metadata comes from public exchange endpoints (no credentials). Every run
persists one immutable verdict per instrument so exclusions stay explainable.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

import httpx
import numpy as np
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.decimal_math import decimal
from app.core.time import TimeService
from app.models import (
    Asset,
    AssetEligibility,
    AssetInstrument,
    MarketCandle,
    MarketDataValidationFailure,
)

LEVERAGED_TOKEN = re.compile(r"(UP|DOWN|BULL|BEAR)$")


@dataclass
class InstrumentInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    spot_trading_allowed: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketStats:
    symbol: str
    last_price: float
    quote_volume_24h: float
    change_24h_pct: float
    trade_count_24h: int = 0
    bid: float | None = None
    ask: float | None = None

    @property
    def spread_bps(self) -> float | None:
        if not self.bid or not self.ask or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 10_000


class UniverseProvider(Protocol):
    name: str

    async def instruments(self) -> list[InstrumentInfo]: ...
    async def market_stats(self) -> dict[str, MarketStats]: ...


def _precision(step: Decimal) -> int:
    normalized = step.normalize()
    exponent = normalized.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


class BinanceUniverseProvider:
    """Public Binance Spot metadata: exchangeInfo, 24h tickers and book tickers."""

    name = "binance"

    def __init__(self, base_url: str = "https://api.binance.com", timeout: float = 20) -> None:
        self.base_url = base_url
        self.timeout = timeout

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def parse_exchange_info(payload: dict[str, Any]) -> list[InstrumentInfo]:
        result: list[InstrumentInfo] = []
        for item in payload.get("symbols", []):
            filters = {entry.get("filterType"): entry for entry in item.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot = filters.get("LOT_SIZE", {})
            notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            try:
                result.append(InstrumentInfo(
                    symbol=item["symbol"], base_asset=item["baseAsset"], quote_asset=item["quoteAsset"],
                    status=item.get("status", "UNKNOWN"),
                    tick_size=decimal(price_filter.get("tickSize", "0")),
                    step_size=decimal(lot.get("stepSize", "0")),
                    min_quantity=decimal(lot.get("minQty", "0")),
                    min_notional=decimal(notional.get("minNotional", "0")),
                    spot_trading_allowed=bool(item.get("isSpotTradingAllowed", True)),
                    raw={"permissions": item.get("permissions", []), "permissionSets": item.get("permissionSets", [])},
                ))
            except (KeyError, ArithmeticError):
                continue
        return result

    @staticmethod
    def parse_stats(tickers: list[dict[str, Any]], books: list[dict[str, Any]]) -> dict[str, MarketStats]:
        book = {item["symbol"]: item for item in books if "symbol" in item}
        stats: dict[str, MarketStats] = {}
        for item in tickers:
            try:
                symbol = item["symbol"]
                quote = book.get(symbol, {})
                stats[symbol] = MarketStats(
                    symbol=symbol, last_price=float(item["lastPrice"]),
                    quote_volume_24h=float(item.get("quoteVolume", 0)),
                    change_24h_pct=float(item.get("priceChangePercent", 0)),
                    trade_count_24h=int(item.get("count", 0)),
                    bid=float(quote["bidPrice"]) if quote.get("bidPrice") else None,
                    ask=float(quote["askPrice"]) if quote.get("askPrice") else None,
                )
            except (KeyError, ValueError, TypeError):
                continue
        return stats

    async def instruments(self) -> list[InstrumentInfo]:
        return self.parse_exchange_info(await self._get("/api/v3/exchangeInfo"))

    async def market_stats(self) -> dict[str, MarketStats]:
        tickers = await self._get("/api/v3/ticker/24hr")
        books = await self._get("/api/v3/ticker/bookTicker")
        return self.parse_stats(tickers, books)


@dataclass
class EligibilityVerdict:
    symbol: str
    eligible: bool
    reasons: list[str]
    metrics: dict[str, Any]


class EligibilityEngine:
    """Pure eligibility rules; persistence and data-quality lookups live in UniverseService."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.quotes = {item.upper() for item in settings.csv("universe_quote_assets")}
        self.excluded = {item.upper() for item in settings.csv("universe_exclude_bases")}
        self.forced = {item.upper() for item in settings.csv("universe_include_symbols")}

    def evaluate(
        self, instrument: InstrumentInfo, stats: MarketStats | None, *, history_candles: int,
        recent_quality_failures: int, volatility: float | None,
    ) -> EligibilityVerdict:
        reasons: list[str] = []
        metrics: dict[str, Any] = {
            "history_candles": history_candles, "recent_quality_failures": recent_quality_failures,
            "volatility": volatility, "tick_size": str(instrument.tick_size),
            "step_size": str(instrument.step_size), "min_notional": str(instrument.min_notional),
        }
        if instrument.status != "TRADING":
            reasons.append("NOT_TRADING")
        if not instrument.spot_trading_allowed:
            reasons.append("SPOT_TRADING_NOT_ALLOWED")
        if instrument.quote_asset.upper() not in self.quotes:
            reasons.append("UNSUPPORTED_QUOTE")
        if instrument.base_asset.upper() in self.excluded:
            reasons.append("EXCLUDED_BASE_ASSET")
        if LEVERAGED_TOKEN.search(instrument.base_asset.upper()) and len(instrument.base_asset) > 4:
            reasons.append("LEVERAGED_TOKEN")
        if instrument.tick_size <= 0 or instrument.step_size <= 0:
            reasons.append("INVALID_INSTRUMENT_FILTERS")
        if stats is None:
            reasons.append("NO_MARKET_STATISTICS")
        else:
            spread = stats.spread_bps
            metrics.update({
                "last_price": stats.last_price, "quote_volume_24h": stats.quote_volume_24h,
                "change_24h_pct": stats.change_24h_pct, "trade_count_24h": stats.trade_count_24h,
                "spread_bps": round(spread, 3) if spread is not None else None,
            })
            if stats.last_price <= 0:
                reasons.append("INVALID_PRICE")
            if stats.quote_volume_24h < self.settings.universe_min_quote_volume_24h:
                reasons.append("ILLIQUID")
            if spread is None:
                reasons.append("SPREAD_UNAVAILABLE")
            elif spread > self.settings.universe_max_spread_bps:
                reasons.append("SPREAD_TOO_WIDE")
            if abs(stats.change_24h_pct) > self.settings.universe_max_abs_change_24h_pct:
                reasons.append("ABNORMAL_MARKET_MOVE")
        if recent_quality_failures > 0:
            reasons.append("RECENT_DATA_QUALITY_FAILURES")
        # History is informational at discovery time: newly eligible assets are backfilled.
        if history_candles < self.settings.universe_min_history_candles:
            metrics["history_status"] = "INSUFFICIENT_HISTORY"
        if instrument.symbol.upper() in self.forced and not {
            "NOT_TRADING", "INVALID_INSTRUMENT_FILTERS", "INVALID_PRICE"
        } & set(reasons):
            metrics["forced_include"] = True
            reasons = [reason for reason in reasons if reason in {"SPOT_TRADING_NOT_ALLOWED"}]
        return EligibilityVerdict(instrument.symbol, not reasons, reasons, metrics)


class UniverseService:
    def __init__(self, session: Session, settings: Settings, provider: UniverseProvider) -> None:
        self.session = session
        self.settings = settings
        self.provider = provider
        self.engine = EligibilityEngine(settings)

    async def refresh(self, timeframe: str = "1h") -> dict[str, Any]:
        instruments = await self.provider.instruments()
        stats = await self.provider.market_stats()
        exchange = self.provider.name
        run_id = str(uuid4())
        verdicts: list[tuple[InstrumentInfo, EligibilityVerdict]] = []
        since = TimeService.now() - timedelta(days=1)
        failures = dict(self.session.execute(
            select(MarketDataValidationFailure.symbol, func.count())
            .where(MarketDataValidationFailure.exchange == exchange, MarketDataValidationFailure.created_at >= since)
            .group_by(MarketDataValidationFailure.symbol)
        ).all())
        history = dict(self.session.execute(
            select(MarketCandle.symbol, func.count())
            .where(MarketCandle.exchange == exchange, MarketCandle.timeframe == timeframe)
            .group_by(MarketCandle.symbol)
        ).all())
        for instrument in instruments:
            if instrument.quote_asset.upper() not in self.engine.quotes:
                continue  # never persist thousands of irrelevant pairs
            verdict = self.engine.evaluate(
                instrument, stats.get(instrument.symbol),
                history_candles=int(history.get(instrument.symbol, 0)),
                recent_quality_failures=int(failures.get(instrument.symbol, 0)),
                volatility=self._volatility(exchange, instrument.symbol, timeframe),
            )
            verdicts.append((instrument, verdict))
        eligible = sorted(
            (item for item in verdicts if item[1].eligible),
            key=lambda item: -float(item[1].metrics.get("quote_volume_24h") or 0),
        )
        ranked = {item[0].symbol: index + 1 for index, item in enumerate(eligible)}
        selected = set(list(ranked)[: self.settings.universe_max_assets])
        for instrument, verdict in verdicts:
            if verdict.eligible and instrument.symbol not in selected:
                verdict.eligible = False
                verdict.reasons.append("OUTSIDE_MAX_UNIVERSE_SIZE")
            self.session.add(AssetEligibility(
                run_id=run_id, exchange=exchange, symbol=instrument.symbol,
                base_asset=instrument.base_asset, quote_asset=instrument.quote_asset,
                eligible=verdict.eligible, reasons=verdict.reasons, metrics=verdict.metrics,
                rank=ranked.get(instrument.symbol),
            ))
            if instrument.symbol in selected:
                self._upsert_instrument(exchange, instrument)
        self.session.commit()
        return {
            "run_id": run_id, "exchange": exchange, "evaluated": len(verdicts),
            "eligible": sorted(selected), "excluded": len(verdicts) - len(selected),
        }

    def _volatility(self, exchange: str, symbol: str, timeframe: str) -> float | None:
        closes = self.session.scalars(select(MarketCandle.close).where(
            MarketCandle.exchange == exchange, MarketCandle.symbol == symbol,
            MarketCandle.timeframe == timeframe,
        ).order_by(desc(MarketCandle.timestamp)).limit(100)).all()
        if len(closes) < 20:
            return None
        values = np.array([float(item) for item in reversed(closes)])
        return round(float(np.std(np.diff(values) / values[:-1])), 6)

    def _upsert_instrument(self, exchange: str, info: InstrumentInfo) -> None:
        asset = self.session.scalar(select(Asset).where(Asset.symbol == info.symbol))
        if not asset:
            asset = Asset(
                base_asset=info.base_asset, quote_asset=info.quote_asset, symbol=info.symbol,
                tick_size=info.tick_size, step_size=info.step_size,
                min_quantity=info.min_quantity, min_notional=info.min_notional,
                exchange_mappings={exchange: info.symbol, "paper": info.symbol},
            )
            self.session.add(asset)
            self.session.flush()
        # The paper venue mirrors the real venue's constraints so simulated orders face them.
        for venue in (exchange, "paper"):
            instrument = self.session.scalar(select(AssetInstrument).where(
                AssetInstrument.exchange == venue, AssetInstrument.exchange_symbol == info.symbol
            ))
            values = {
                "tick_size": info.tick_size, "step_size": info.step_size,
                "min_quantity": info.min_quantity, "min_notional": info.min_notional,
                "price_precision": _precision(info.tick_size),
                "quantity_precision": _precision(info.step_size),
                "trading_status": "TRADING" if info.status == "TRADING" else info.status,
                "metadata_payload": {"source": exchange, **info.raw, "mirrored": venue == "paper"},
            }
            if instrument:
                for key, value in values.items():
                    setattr(instrument, key, value)
            else:
                self.session.add(AssetInstrument(
                    asset_id=asset.id, exchange=venue, exchange_symbol=info.symbol,
                    contract_type="spot", **values,
                ))

    @staticmethod
    def latest_run(session: Session, exchange: str) -> str | None:
        return session.scalar(select(AssetEligibility.run_id).where(
            AssetEligibility.exchange == exchange
        ).order_by(desc(AssetEligibility.evaluated_at)).limit(1))

    @staticmethod
    def eligible_symbols(session: Session, exchange: str) -> list[str]:
        run_id = UniverseService.latest_run(session, exchange)
        if not run_id:
            return []
        rows = session.scalars(select(AssetEligibility).where(
            AssetEligibility.run_id == run_id, AssetEligibility.eligible.is_(True)
        ).order_by(AssetEligibility.rank)).all()
        return [row.symbol for row in rows]

    @staticmethod
    def is_eligible(session: Session, exchange: str, symbol: str) -> bool:
        return symbol in UniverseService.eligible_symbols(session, exchange)
