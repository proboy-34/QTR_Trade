"""Mocked-provider tests (offline, deterministic). Real-provider checks are run separately via
POST /api/v1/system/providers/verify and recorded as ProviderCheck rows."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import JsonFormatter, redact
from app.integrations.fred import MacroService, macro_as_of, parse_vintages
from app.integrations.health import ProviderVerifier, provider_health, provider_state, system_health
from app.models import MacroObservation, ProviderCheck

KEYS = {"binance_api_key": "binance-key-000", "finnhub_api_key": "finnhub-secret-123", "fred_api_key": "fred-secret-456", "gemini_api_key": "gemini-secret-789"}


async def no_sleep(_: float) -> None:
    return None


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def healthy_handler(request: httpx.Request) -> httpx.Response:
    url, path = str(request.url), request.url.path
    if "binance" in url:
        return {
            "/api/v3/ping": httpx.Response(200, json={}),
            "/api/v3/time": httpx.Response(200, json={"serverTime": now_ms()}),
            "/api/v3/exchangeInfo": httpx.Response(200, json={"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}),
            "/api/v3/ticker/bookTicker": httpx.Response(200, json={"bidPrice": "100.0", "askPrice": "100.01"}),
            "/api/v3/klines": httpx.Response(200, json=[[now_ms() - 60_000, "1", "2", "0.5", "1.5", "10"]] * 3),
        }[path]
    if "googleapis" in url:
        if path.endswith(":generateContent"):
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}],
                                             "usageMetadata": {"totalTokenCount": 30}, "modelVersion": "m"})
        return httpx.Response(200, json={"displayName": "Model"})
    if "finnhub" in url:
        if path.endswith("/news"):
            return httpx.Response(200, json=[{"id": 1, "headline": "h", "datetime": now_ms() // 1000}], headers={"x-ratelimit-remaining": "59"})
        return httpx.Response(403, json={"error": "You don't have access to this resource."})
    if "stlouisfed" in url:
        if request.url.params.get("realtime_start") == "1776-07-04":
            return httpx.Response(200, json={"observations": [{"date": "2024-01-01", "value": "1", "realtime_start": "2024-02-13", "realtime_end": "9999-12-31"}]})
        return httpx.Response(200, json={"observations": [{"date": "2026-09-24", "value": "4.1", "realtime_start": "2026-09-25", "realtime_end": "9999-12-31"}]})
    raise AssertionError(url)


@pytest.mark.asyncio
async def test_verification_records_real_states_including_plan_restrictions(session):
    settings = Settings(**KEYS, binance_public_base_url="https://data-api.binance.vision")
    result = await ProviderVerifier(settings, httpx.MockTransport(healthy_handler), sleep=no_sleep).run(session)
    providers = result["providers"]
    # Public market data and authenticated account access are separate components: the key
    # cannot be verified without its secret, which never degrades public data.
    assert providers["binance"]["state"] == "HEALTHY"
    assert providers["binance_account"]["state"] == "NOT_CONFIGURED"
    assert "BINANCE_API_SECRET" in providers["binance_account"]["checks"][0]["detail"]
    assert providers["gemini"]["state"] == "HEALTHY"
    # News works but the economic calendar is not on this plan: reported, not pretended.
    calendar = next(c for c in providers["finnhub"]["checks"] if c["check"] == "economic_calendar")
    assert providers["finnhub"]["state"] == "DEGRADED" and calendar["result"] == "NOT_AVAILABLE_ON_PLAN"
    assert providers["fred"]["state"] == "HEALTHY"
    rows = session.scalars(select(ProviderCheck)).all()
    assert rows and all(row.latency_ms is not None or row.result in ("NOT_CONFIGURED", "SKIPPED") for row in rows)
    stored = " ".join(row.detail + json.dumps(row.data) for row in rows)
    assert not any(secret in stored for secret in KEYS.values())


@pytest.mark.asyncio
async def test_listed_model_that_cannot_generate_is_unavailable_and_transient_errors_retry(session):
    calls = {"generate": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":generateContent"):
            calls["generate"] += 1
            return httpx.Response(404, json={"error": {"message": "This model is no longer available to new users."}})
        return httpx.Response(200, json={"displayName": "Listed model"})

    result = await ProviderVerifier(Settings(**KEYS), httpx.MockTransport(handler), sleep=no_sleep).run(session, ("gemini",))
    assert result["providers"]["gemini"]["state"] == "UNAVAILABLE" and calls["generate"] == 1  # 404 is not retried
    attempts = iter([503, 200])

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":generateContent"):
            code = next(attempts)
            return httpx.Response(code, json={"candidates": [{"content": {"parts": [{"text": '{"status":"ok"}'}]}}]} if code == 200 else {})
        return httpx.Response(200, json={"displayName": "Model"})

    retried = await ProviderVerifier(Settings(**KEYS), httpx.MockTransport(flaky), sleep=no_sleep).run(session, ("gemini",))
    generation = next(c for c in retried["providers"]["gemini"]["checks"] if c["check"] == "structured_generation")
    assert generation["result"] == "OK" and generation["data"]["attempts"] == 2


@pytest.mark.asyncio
async def test_network_failures_malformed_payloads_and_missing_keys(session):
    def broken(request: httpx.Request) -> httpx.Response:
        if "finnhub" in str(request.url):
            raise httpx.ConnectError(f"refused for {request.url}")  # the URL carries the token
        if "stlouisfed" in str(request.url):
            return httpx.Response(200, json={"unexpected": True})
        return httpx.Response(200, text="not json")

    result = await ProviderVerifier(Settings(**KEYS, binance_public_base_url="https://data-api.binance.vision"),
                                    httpx.MockTransport(broken), sleep=no_sleep).run(session, ("finnhub", "fred", "binance"))
    assert all(result["providers"][name]["state"] == "UNAVAILABLE" for name in ("finnhub", "fred", "binance"))
    news = result["providers"]["finnhub"]["checks"][0]
    assert "finnhub-secret-123" not in news["detail"] and "token=••••" in news["detail"]
    assert "Malformed" in result["providers"]["fred"]["checks"][0]["detail"]
    unconfigured = await ProviderVerifier(Settings(), httpx.MockTransport(broken)).run(session, ("gemini", "finnhub", "fred"))
    assert {item["state"] for item in unconfigured["providers"].values()} == {"NOT_CONFIGURED"}


def test_health_is_not_verified_before_checks_and_stale_after(session):
    settings = Settings(**KEYS, provider_check_interval_seconds=60)
    assert provider_health(session, settings, "fred")["state"] == "NOT_VERIFIED"
    session.add(ProviderCheck(run_id="r", provider="fred", check="series_observations", endpoint="/x", required=True,
                              result="OK", checked_at=datetime.now(UTC) - timedelta(hours=1)))
    session.commit()
    assert provider_health(session, settings, "fred")["state"] == "STALE"

    class Bus:
        healthy, published, failures = True, 0, 0

    report = system_health(session, settings, {}, {"running": False}, Bus())
    assert report["overall"] == "UNAVAILABLE"  # no market data, no scheduler: never "healthy" by default
    assert report["components"]["market_data"]["state"] == "UNAVAILABLE"
    assert report["components"]["binance"]["state"] == "NOT_VERIFIED"
    assert report["components"]["news"]["state"] == "NOT_VERIFIED"


def test_provider_state_rules():
    from app.integrations.health import CheckOutcome

    assert provider_state([CheckOutcome("a", "/", "OK"), CheckOutcome("b", "/", "SKIPPED", required=False)]) == "DEGRADED"
    assert provider_state([CheckOutcome("a", "/", "FAILED")]) == "UNAVAILABLE"
    assert provider_state([CheckOutcome("a", "/", "OK")]) == "HEALTHY"


def test_secrets_are_redacted_from_logs_and_query_strings(capsys):
    secrets = ["super-secret-value"]
    assert redact("key super-secret-value in text", secrets) == "key •••• in text"
    assert redact("GET https://x/y?series_id=A&api_key=abc123&file_type=json", []) == "GET https://x/y?series_id=A&api_key=••••&file_type=json"
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "GET https://finnhub.io/api/v1/news?token=abcdef", None, None)
    assert "abcdef" not in JsonFormatter().format(record)
    assert "gemini-secret-789" in Settings(**KEYS).secret_values()
    assert logging.getLogger("httpx").level in (logging.NOTSET, logging.WARNING)


def test_fred_vintages_are_point_in_time(session):
    payload = {"observations": [
        # March CPI first published 2024-04-10 as 100, revised on 2024-05-15 to 101.
        {"date": "2024-03-01", "value": "100", "realtime_start": "2024-04-10", "realtime_end": "2024-05-14"},
        {"date": "2024-03-01", "value": "101", "realtime_start": "2024-05-15", "realtime_end": "9999-12-31"},
        {"date": "2024-04-01", "value": "102", "realtime_start": "2024-05-15", "realtime_end": "9999-12-31"},
        {"date": "2024-05-01", "value": ".", "realtime_start": "2024-06-12", "realtime_end": "9999-12-31"},
    ]}
    rows = parse_vintages("CPIAUCSL", payload, "Index", "CPI")
    assert rows[0]["available_at"] == datetime(2024, 4, 11, tzinfo=UTC) and rows[3]["value"] is None
    service = MacroService(session, Settings(fred_api_key="k"))
    assert service.store(rows) == 4 and service.store(rows) == 0  # idempotent
    session.commit()
    before_release = macro_as_of(session, datetime(2024, 4, 10, 20, tzinfo=UTC))
    assert "CPIAUCSL" not in before_release  # published that day; not usable until it is knowable
    first = macro_as_of(session, datetime(2024, 4, 20, tzinfo=UTC))["CPIAUCSL"]
    assert first["observation_date"] == "2024-03-01" and first["value"] == 100.0  # original vintage, not the revision
    later = macro_as_of(session, datetime(2024, 5, 20, tzinfo=UTC))["CPIAUCSL"]
    assert later["observation_date"] == "2024-04-01" and later["value"] == 102.0 and later["previous_value"] == 101.0
    assert "vintage" in later["provenance"] and session.scalar(select(MacroObservation.retrieved_at)) is not None


@pytest.mark.asyncio
async def test_fred_ingestion_failure_is_reported_without_the_key(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error_message": "Too Many Requests"})

    from app.integrations.fred import FredClient

    settings = Settings(fred_api_key="fred-secret-456", fred_series="DGS10")
    report = await MacroService(session, settings, FredClient(settings, httpx.MockTransport(handler))).ingest()
    assert "error" in report["DGS10"] and "fred-secret-456" not in report["DGS10"]["error"]
    assert (await MacroService(session, Settings()).ingest())["skipped"] == "FRED_API_KEY not configured"
