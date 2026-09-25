"""Read-only verification of a mainnet Binance API key.

Only the two GET endpoints below may ever be called with mainnet credentials. This module
cannot place, cancel or transfer anything: the method and path are checked against an
allowlist before any request is signed.
"""

import hashlib
import hmac
from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.core.logging import redact
from app.core.time import TimeService

READ_ONLY_ENDPOINTS = frozenset({("GET", "/api/v3/account"), ("GET", "/sapi/v1/account/apiRestrictions")})


async def _signed_get(settings: Settings, path: str, transport: httpx.AsyncBaseTransport | None, timeout: float) -> tuple[int, Any, int]:
    if ("GET", path) not in READ_ONLY_ENDPOINTS:
        raise PermissionError(f"{path} is not an allowed read-only endpoint")
    query = urlencode({"timestamp": int(TimeService.now().timestamp() * 1000), "recvWindow": 5000})
    signature = hmac.new(settings.binance_api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    started = perf_counter()
    async with httpx.AsyncClient(base_url=settings.binance_public_base_url, timeout=timeout, transport=transport) as client:
        response = await client.get(f"{path}?{query}&signature={signature}", headers={"X-MBX-APIKEY": settings.binance_api_key})
    return response.status_code, response.json() if response.content else {}, round((perf_counter() - started) * 1000)


async def verify_account(settings: Settings, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20) -> list:
    from app.integrations.health import CheckOutcome

    outcomes = []
    for check, path in (("account_read", "/api/v3/account"), ("key_permissions", "/sapi/v1/account/apiRestrictions")):
        try:
            status, payload, latency = await _signed_get(settings, path, transport, timeout)
        except (httpx.HTTPError, ValueError) as exc:
            outcomes.append(CheckOutcome(check, path, "FAILED", False, None, None, redact(str(exc), settings.secret_values())[:300]))
            continue
        if status >= 400:
            outcomes.append(CheckOutcome(check, path, "FAILED", False, status, latency, redact(str(payload), settings.secret_values())[:300]))
            continue
        if check == "account_read":
            data = {"can_trade": payload.get("canTrade"), "account_type": payload.get("accountType")}
            detail = f"key valid; account type {data['account_type']}"
            outcomes.append(CheckOutcome(check, path, "OK", False, status, latency, detail, data))
        else:
            data = {key: payload.get(key) for key in ("enableReading", "enableSpotAndMarginTrading", "enableWithdrawals",
                                                      "enableInternalTransfer", "ipRestrict", "enableFutures")}
            risky = [key for key in ("enableWithdrawals", "enableInternalTransfer") if data.get(key)]
            detail = ("UNSAFE: disable " + ", ".join(risky)) if risky else "withdrawals and transfers disabled"
            outcomes.append(CheckOutcome(check, path, "FAILED" if risky else "OK", False, status, latency, detail, data))
    return outcomes
