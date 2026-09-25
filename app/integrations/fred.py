"""FRED macro data stored point-in-time (ALFRED vintages).

FRED returns every vintage of an observation with the date range in which that value was
the published one (realtime_start..realtime_end). Storing vintages lets a decision or a
backtest at time T see only what was public at T — never a later revision or a value
released after T. FRED dates have no time of day, so a vintage is treated as available
from 00:00 UTC on the day after realtime_start (conservative: US releases occur during
the realtime_start day).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import redact
from app.core.time import TimeService
from app.models import MacroObservation

BASE_URL = "https://api.stlouisfed.org/fred"
OPEN_END = "9999-12-31"


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def availability(realtime_start: str) -> datetime:
    return _date(realtime_start) + timedelta(days=1)


def parse_vintages(series_id: str, payload: dict[str, Any], units: str | None = None, title: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for item in payload.get("observations", []):
        try:
            value = None if item["value"] in {".", ""} else Decimal(item["value"])
        except (InvalidOperation, KeyError):
            continue
        rows.append({
            "series_id": series_id, "observation_date": _date(item["date"]), "value": value,
            "realtime_start": _date(item["realtime_start"]),
            "realtime_end": None if item["realtime_end"] == OPEN_END else _date(item["realtime_end"]),
            "available_at": availability(item["realtime_start"]), "units": units, "title": title,
        })
    return rows


class FredClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {**params, "api_key": self.settings.fred_api_key, "file_type": "json"}
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=30, transport=self.transport) as client:
                response = await client.get(path, params=query)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:  # never leak the api_key query parameter
            raise ConnectionError(redact(f"FRED {path} failed: {exc}", self.settings.secret_values())) from None

    async def series_info(self, series_id: str) -> dict[str, Any]:
        payload = await self._get("/series", {"series_id": series_id})
        return (payload.get("seriess") or [{}])[0]

    async def vintages(self, series_id: str, start: datetime) -> list[dict[str, Any]]:
        info = await self.series_info(series_id)
        payload = await self._get("/series/observations", {
            "series_id": series_id, "observation_start": start.date().isoformat(),
            "realtime_start": "1776-07-04", "realtime_end": OPEN_END,
        })
        return parse_vintages(series_id, payload, info.get("units_short") or info.get("units"), info.get("title"))


class MacroService:
    def __init__(self, session: Session, settings: Settings, client: FredClient | None = None) -> None:
        self.session = session
        self.settings = settings
        self.client = client or FredClient(settings)

    async def ingest(self) -> dict[str, Any]:
        if not self.settings.fred_api_key:
            return {"skipped": "FRED_API_KEY not configured"}
        start = TimeService.now() - timedelta(days=365 * self.settings.fred_history_years)
        report: dict[str, Any] = {}
        for series_id in self.settings.csv("fred_series"):
            try:
                rows = await self.client.vintages(series_id, start)
            except ConnectionError as exc:
                report[series_id] = {"error": str(exc)[:300]}
                continue
            report[series_id] = {"received": len(rows), "stored": self.store(rows)}
        self.session.commit()
        return report

    def store(self, rows: list[dict[str, Any]]) -> int:
        stored = 0
        for row in rows:
            key = and_(MacroObservation.source == "fred", MacroObservation.series_id == row["series_id"],
                       MacroObservation.observation_date == row["observation_date"],
                       MacroObservation.realtime_start == row["realtime_start"])
            existing = self.session.scalar(select(MacroObservation).where(key))
            if existing:
                # A vintage's value is immutable; only its end date can become known later.
                if existing.realtime_end is None and row["realtime_end"] is not None:
                    existing.realtime_end = row["realtime_end"]
                continue
            self.session.add(MacroObservation(source="fred", **row))
            stored += 1
        self.session.flush()
        return stored


def vintage_valid_at(row: MacroObservation, at: datetime) -> bool:
    """Public from available_at until the next vintage becomes public (realtime_end + 2 days)."""
    if TimeService.ensure_utc(row.available_at) > at:
        return False
    return row.realtime_end is None or TimeService.ensure_utc(row.realtime_end) + timedelta(days=2) > at


def macro_as_of(session: Session, at: datetime, series: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Latest observation per series using only vintages public at `at` (no look-ahead)."""
    at = TimeService.ensure_utc(at)
    names = series or list(session.scalars(select(MacroObservation.series_id).distinct()).all())
    result: dict[str, dict[str, Any]] = {}
    for series_id in names:
        rows = session.scalars(select(MacroObservation).where(
            MacroObservation.series_id == series_id, MacroObservation.available_at <= at,
        ).order_by(desc(MacroObservation.observation_date), desc(MacroObservation.realtime_start)).limit(60)).all()
        by_date: dict[datetime, MacroObservation] = {}
        for row in rows:
            if vintage_valid_at(row, at):
                by_date.setdefault(TimeService.ensure_utc(row.observation_date), row)
        dates = sorted(by_date, reverse=True)
        if not dates:
            continue
        latest, previous = by_date[dates[0]], (by_date[dates[1]] if len(dates) > 1 else None)
        result[series_id] = {
            "series_id": series_id, "title": latest.title, "units": latest.units,
            "observation_date": dates[0].date().isoformat(),
            "value": None if latest.value is None else float(latest.value),
            "previous_value": None if previous is None or previous.value is None else float(previous.value),
            "available_at": TimeService.ensure_utc(latest.available_at).isoformat(),
            "source": "FRED",
            "provenance": f"fred:{series_id}:{dates[0].date()}:vintage {TimeService.ensure_utc(latest.realtime_start).date()}",
        }
    return result
