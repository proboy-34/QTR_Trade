"""Real provider verification and truthful health states.

Every check performs a real request (or records exactly why it was not performed) and is
persisted as a ProviderCheck. Provider and system health are derived only from these
records, job executions, data freshness and database access — never assumed.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import redact
from app.core.time import TimeService
from app.models import JobExecution, MarketCandle, ProviderCheck

HEALTHY, DEGRADED, UNAVAILABLE, STALE, NOT_CONFIGURED, NOT_VERIFIED = (
    "HEALTHY", "DEGRADED", "UNAVAILABLE", "STALE", "NOT_CONFIGURED", "NOT_VERIFIED",
)
PROVIDERS = ("binance", "gemini", "finnhub", "fred")


@dataclass
class CheckOutcome:
    check: str
    endpoint: str
    result: str
    required: bool = True
    http_status: int | None = None
    latency_ms: int | None = None
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class ProviderVerifier:
    """Runs real checks. `transport` exists only so tests can run offline."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.settings = settings
        self.transport = transport
        self.timeout = timeout
        self.sleep = sleep

    def _redact(self, text: str) -> str:
        return redact(text, self.settings.secret_values())

    async def _get(self, check: str, base: str, path: str, *, params: dict | None = None, headers: dict | None = None,
                   required: bool = True, method: str = "GET", body: dict | None = None,
                   plan_codes: tuple[int, ...] = (), validate: Callable[[Any, httpx.Response], dict[str, Any]] | None = None,
                   transient_retries: int = 1) -> CheckOutcome:
        outcome = await self._attempt(check, base, path, params, headers, required, method, body, plan_codes, validate)
        for attempt in range(transient_retries):
            if outcome.http_status not in {429, 500, 502, 503, 504}:
                break
            await self.sleep(2 * (attempt + 1))
            outcome = await self._attempt(check, base, path, params, headers, required, method, body, plan_codes, validate)
            outcome.data["attempts"] = attempt + 2
        return outcome

    async def _attempt(self, check: str, base: str, path: str, params: dict | None, headers: dict | None, required: bool,
                       method: str, body: dict | None, plan_codes: tuple[int, ...],
                       validate: Callable[[Any, httpx.Response], dict[str, Any]] | None) -> CheckOutcome:
        started = perf_counter()
        try:
            async with httpx.AsyncClient(base_url=base, timeout=self.timeout, transport=self.transport) as client:
                response = await client.request(method, path, params=params, headers=headers, json=body)
        except httpx.HTTPError as exc:
            return CheckOutcome(check, path, "FAILED", required, None, round((perf_counter() - started) * 1000),
                                self._redact(f"{type(exc).__name__}: {exc}")[:500])
        latency = round((perf_counter() - started) * 1000)
        if response.status_code in plan_codes:
            return CheckOutcome(check, path, "NOT_AVAILABLE_ON_PLAN", required, response.status_code, latency,
                                self._redact(response.text[:300]))
        if response.status_code >= 400:
            return CheckOutcome(check, path, "FAILED", required, response.status_code, latency,
                                self._redact(response.text[:300]))
        try:
            payload = response.json()
            data = validate(payload, response) if validate else {}
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            return CheckOutcome(check, path, "FAILED", required, response.status_code, latency,
                                self._redact(f"Malformed response: {type(exc).__name__}: {exc}")[:500])
        return CheckOutcome(check, path, "OK", required, response.status_code, latency, data.pop("_detail", ""), data)

    # ------------------------------------------------------------------ providers
    async def binance(self) -> list[CheckOutcome]:
        base = self.settings.binance_public_base_url

        def server_time(payload: Any, _: httpx.Response) -> dict[str, Any]:
            skew = int(payload["serverTime"]) - int(TimeService.now().timestamp() * 1000)
            return {"clock_skew_ms": skew, "_detail": f"clock skew {skew} ms"}

        def symbol(payload: Any, _: httpx.Response) -> dict[str, Any]:
            item = payload["symbols"][0]
            return {"symbol": item["symbol"], "status": item["status"], "_detail": f"{item['symbol']} {item['status']}"}

        def book(payload: Any, _: httpx.Response) -> dict[str, Any]:
            bid, ask = float(payload["bidPrice"]), float(payload["askPrice"])
            if bid <= 0 or ask < bid:
                raise ValueError("invalid bid/ask")
            spread = (ask - bid) / ((ask + bid) / 2) * 10_000
            return {"bid": bid, "ask": ask, "spread_bps": round(spread, 4), "_detail": f"spread {spread:.3f} bps"}

        def klines(payload: Any, _: httpx.Response) -> dict[str, Any]:
            if not payload or len(payload[0]) < 6:
                raise ValueError("empty or malformed klines")
            last_open = int(payload[-1][0])
            age = int(TimeService.now().timestamp() * 1000) - last_open
            return {"candles": len(payload), "latest_open_age_ms": age, "_detail": f"{len(payload)} candles, latest opened {age // 1000}s ago"}

        checks = [
            await self._get("ping", base, "/api/v3/ping"),
            await self._get("server_time", base, "/api/v3/time", validate=server_time),
            await self._get("exchange_info", base, "/api/v3/exchangeInfo", params={"symbol": "BTCUSDT"}, validate=symbol),
            await self._get("book_ticker", base, "/api/v3/ticker/bookTicker", params={"symbol": "BTCUSDT"}, validate=book),
            await self._get("klines", base, "/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "1m", "limit": 3}, validate=klines),
        ]
        if not self.settings.binance_api_key:
            checks.append(CheckOutcome("api_key", "/api/v3/account", "NOT_CONFIGURED", False,
                                       detail="BINANCE_API_KEY not set; public data needs no key"))
        elif not self.settings.binance_api_secret:
            checks.append(CheckOutcome("api_key", "/api/v3/account", "SKIPPED", False,
                                       detail="BINANCE_API_SECRET not set: the key cannot be verified without a signed request"))
        else:
            from app.integrations.binance_account import verify_account

            checks.extend(await verify_account(self.settings, self.transport, self.timeout))
        return checks

    async def gemini(self, generate: bool = True) -> list[CheckOutcome]:
        if not self.settings.gemini_api_key:
            return [CheckOutcome("api_key", "/models", "NOT_CONFIGURED", detail="GEMINI_API_KEY not set")]
        base = self.settings.gemini_base_url.rstrip("/")
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        model = self.settings.gemini_model
        checks = [await self._get("model_metadata", base, f"/models/{model}", headers=headers,
                                  validate=lambda p, _: {"_detail": p.get("displayName", model)})]
        if not generate:
            return checks

        def structured(payload: Any, _: httpx.Response) -> dict[str, Any]:
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
            parsed = json.loads(text)
            if parsed.get("status") != "ok":
                raise ValueError("unexpected structured content")
            usage = payload.get("usageMetadata", {})
            return {"model_version": payload.get("modelVersion"), "usage": usage,
                    "_detail": f"structured JSON ok, {usage.get('totalTokenCount', '?')} tokens"}

        # A listed model is not proof of availability: only a real generation proves it.
        checks.append(await self._get(
            "structured_generation", base, f"/models/{model}:generateContent", headers=headers, method="POST",
            body={"systemInstruction": {"parts": [{"text": "Answer only with JSON."}]},
                  "contents": [{"role": "user", "parts": [{"text": 'Return {"status":"ok"}'}]}],
                  "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 400, "temperature": 0}},
            validate=structured))
        return checks

    async def finnhub(self) -> list[CheckOutcome]:
        if not self.settings.finnhub_api_key:
            return [CheckOutcome("api_key", "/news", "NOT_CONFIGURED", detail="FINNHUB_API_KEY not set")]
        base, token = "https://finnhub.io/api/v1", {"token": self.settings.finnhub_api_key}

        def news(payload: Any, response: httpx.Response) -> dict[str, Any]:
            if not isinstance(payload, list):
                raise ValueError("news payload is not a list")
            latest = max((int(item.get("datetime", 0)) for item in payload), default=0)
            age_h = (TimeService.now().timestamp() - latest) / 3600 if latest else None
            return {"articles": len(payload), "latest_age_hours": round(age_h, 2) if age_h is not None else None,
                    "rate_limit_remaining": response.headers.get("x-ratelimit-remaining"),
                    "_detail": f"{len(payload)} articles" + (f", newest {age_h:.1f}h old" if age_h is not None else ", empty")}

        def calendar(payload: Any, _: httpx.Response) -> dict[str, Any]:
            events = payload.get("economicCalendar", [])
            return {"events": len(events), "_detail": f"{len(events)} calendar events"}

        return [
            await self._get("crypto_news", base, "/news", params={"category": "crypto", **token}, validate=news, plan_codes=(401, 403)),
            await self._get("economic_calendar", base, "/calendar/economic", params=token, validate=calendar,
                            required=False, plan_codes=(401, 403)),
        ]

    async def fred(self) -> list[CheckOutcome]:
        if not self.settings.fred_api_key:
            return [CheckOutcome("api_key", "/series/observations", "NOT_CONFIGURED", detail="FRED_API_KEY not set")]
        base = "https://api.stlouisfed.org/fred"
        params = {"api_key": self.settings.fred_api_key, "file_type": "json"}

        def observations(payload: Any, _: httpx.Response) -> dict[str, Any]:
            item = payload["observations"][0]
            return {"date": item["date"], "value": item["value"], "realtime_start": item["realtime_start"],
                    "_detail": f"DGS10 {item['date']} = {item['value']}"}

        def vintages(payload: Any, _: httpx.Response) -> dict[str, Any]:
            rows = payload["observations"]
            return {"vintages": len(rows), "_detail": f"{len(rows)} vintages returned (point-in-time data available)"}

        return [
            await self._get("series_observations", base, "/series/observations",
                            params={**params, "series_id": "DGS10", "sort_order": "desc", "limit": 1}, validate=observations),
            await self._get("vintage_history", base, "/series/observations",
                            params={**params, "series_id": "CPIAUCSL", "observation_start": "2024-01-01",
                                    "observation_end": "2024-01-01", "realtime_start": "1776-07-04",
                                    "realtime_end": "9999-12-31"}, validate=vintages, required=False),
        ]

    async def run(self, session: Session, providers: tuple[str, ...] = PROVIDERS, generate: bool = True) -> dict[str, Any]:
        run_id = str(uuid4())
        runners: dict[str, Callable[[], Awaitable[list[CheckOutcome]]]] = {
            "binance": self.binance, "gemini": lambda: self.gemini(generate), "finnhub": self.finnhub, "fred": self.fred,
        }
        results: dict[str, Any] = {}
        for provider in providers:
            outcomes = await runners[provider]()
            for outcome in outcomes:
                session.add(ProviderCheck(run_id=run_id, provider=provider, check=outcome.check, endpoint=outcome.endpoint,
                                          required=outcome.required, result=outcome.result, http_status=outcome.http_status,
                                          latency_ms=outcome.latency_ms, detail=outcome.detail, data=outcome.data))
            results[provider] = {"state": provider_state(outcomes), "checks": [outcome.__dict__ for outcome in outcomes]}
        session.commit()
        return {"run_id": run_id, "checked_at": TimeService.now().isoformat(), "providers": results}


def provider_state(outcomes: list[CheckOutcome] | list[ProviderCheck]) -> str:
    required = [item for item in outcomes if item.required]
    if any(item.result == "NOT_CONFIGURED" for item in required):
        return NOT_CONFIGURED
    if any(item.result in {"FAILED", "NOT_AVAILABLE_ON_PLAN"} for item in required):
        return UNAVAILABLE
    optional_problem = any(item.result in {"FAILED", "NOT_AVAILABLE_ON_PLAN", "SKIPPED"} for item in outcomes if not item.required)
    return DEGRADED if optional_problem else HEALTHY


def latest_checks(session: Session, provider: str) -> list[ProviderCheck]:
    run_id = session.scalar(select(ProviderCheck.run_id).where(ProviderCheck.provider == provider)
                            .order_by(desc(ProviderCheck.checked_at)).limit(1))
    if not run_id:
        return []
    return list(session.scalars(select(ProviderCheck).where(ProviderCheck.run_id == run_id,
                                                            ProviderCheck.provider == provider)).all())


def provider_health(session: Session, settings: Settings, provider: str) -> dict[str, Any]:
    checks = latest_checks(session, provider)
    if not checks:
        return {"state": NOT_VERIFIED, "checked_at": None, "checks": [], "note": "No verification has run yet"}
    checked_at = TimeService.ensure_utc(max(item.checked_at for item in checks))
    state = provider_state(checks)
    if state in {HEALTHY, DEGRADED} and TimeService.now() - checked_at > timedelta(seconds=settings.provider_check_interval_seconds * 3):
        state = STALE
    return {"state": state, "checked_at": checked_at.isoformat(), "checks": [
        {"check": item.check, "endpoint": item.endpoint, "required": item.required, "result": item.result,
         "http_status": item.http_status, "latency_ms": item.latency_ms, "detail": item.detail} for item in checks]}


def capability_available(session: Session, provider: str, check: str) -> bool | None:
    """Whether the last verification proved a capability works (None = never verified)."""
    row = session.scalar(select(ProviderCheck).where(ProviderCheck.provider == provider, ProviderCheck.check == check)
                         .order_by(desc(ProviderCheck.checked_at)))
    return None if row is None else row.result == "OK"


def system_health(session: Session, settings: Settings, scheduler_states: dict[str, Any], live_state: dict[str, Any],
                  event_bus: Any) -> dict[str, Any]:
    from app.trading.safety import SafetyService

    components: dict[str, dict[str, Any]] = {}
    try:
        session.execute(select(func.count()).select_from(JobExecution)).scalar()
        components["database"] = {"state": HEALTHY, "detail": session.get_bind().dialect.name}
    except Exception as exc:  # database failure must be visible, not hidden
        components["database"] = {"state": UNAVAILABLE, "detail": type(exc).__name__}
    for provider in PROVIDERS:
        components[provider] = provider_health(session, settings, provider)
    components["ai"] = {**components["gemini"], "detail": f"model {settings.gemini_model}; trade review {settings.ai_trade_review}"}
    last_news = session.scalar(select(JobExecution).where(JobExecution.job_name == "news_ingestion")
                               .order_by(desc(JobExecution.started_at)))
    components["news"] = {
        "state": NOT_CONFIGURED if not settings.finnhub_api_key else (
            NOT_VERIFIED if last_news is None else (HEALTHY if last_news.status == "COMPLETED" and not (last_news.details or {}).get("skipped")
                                                     else UNAVAILABLE)),
        "detail": (last_news.error or json.dumps(last_news.details or {})[:200]) if last_news else "no ingestion run yet"}
    exchange = settings.scanner_exchange
    latest = session.scalar(select(func.max(MarketCandle.timestamp)).where(MarketCandle.exchange == exchange))
    if latest is None:
        components["market_data"] = {"state": UNAVAILABLE, "detail": f"no {exchange} candles stored"}
    else:
        age = TimeService.now() - TimeService.ensure_utc(latest)
        components["market_data"] = {"state": HEALTHY if age <= timedelta(hours=3) else STALE,
                                     "detail": f"latest {exchange} candle opened {int(age.total_seconds() // 60)} min ago"}
    failing = {name: state.get("last_error") for name, state in scheduler_states.items() if state.get("status") == "ERROR"}
    components["scheduler"] = {"state": (UNAVAILABLE if not scheduler_states else DEGRADED if failing else HEALTHY),
                               "detail": f"{len(scheduler_states)} jobs, {len(failing)} failing", "failing": failing}
    components["event_bus"] = {"state": HEALTHY if event_bus.healthy else DEGRADED,
                               "detail": f"{event_bus.published} published, {event_bus.failures} handler failures"}
    halts = SafetyService(session).blocks_new_orders()
    components["execution"] = {"state": DEGRADED if halts else HEALTHY, "mode": settings.execution_mode,
                               "detail": ", ".join(halts) or f"{settings.execution_mode} venue accepting orders"}
    components["risk_engine"] = {"state": HEALTHY, "detail": "deterministic limits loaded from configuration"}
    components["live_stream"] = {"state": (HEALTHY if live_state.get("connected") else UNAVAILABLE) if live_state.get("running") else "STOPPED",
                                 "detail": f"{live_state.get('status')} {','.join(live_state.get('symbols') or [])}"}
    critical = ("database", "binance", "market_data", "scheduler")
    worst = [components[name]["state"] for name in critical]
    overall = UNAVAILABLE if UNAVAILABLE in worst else DEGRADED if any(s != HEALTHY for s in worst) else HEALTHY
    return {"overall": overall, "components": components, "checked_at": TimeService.now().isoformat(),
            "data_mode": "DEMO" if settings.demo_mode else "REAL", "execution_mode": settings.execution_mode.upper()}
