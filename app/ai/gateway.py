"""Budget, rate-limit, retry and audit boundary for every AI call."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.providers import AIProvider, AIProviderError, AIRequest, AIResponse, redact
from app.core.config import Settings
from app.core.decimal_math import decimal, money
from app.core.time import TimeService
from app.models import AICallRecord


class AIBudgetExceeded(Exception):
    pass


class AIUnavailable(Exception):
    pass


@dataclass
class AIResult:
    response: AIResponse
    call: AICallRecord


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class AIGateway:
    def __init__(
        self, session: Session, settings: Settings, provider: AIProvider,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session = session
        self.settings = settings
        self.provider = provider
        self.sleep = sleep

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return money(
            decimal(input_tokens) * decimal(self.settings.ai_input_cost_per_mtok) / 1_000_000
            + decimal(output_tokens) * decimal(self.settings.ai_output_cost_per_mtok) / 1_000_000
        )

    def _spent(self, since: datetime) -> Decimal:
        return decimal(self.session.scalar(
            select(func.coalesce(func.sum(AICallRecord.estimated_cost), 0)).where(AICallRecord.created_at >= since)
        ) or 0)

    def usage(self) -> dict[str, Any]:
        now = TimeService.now()
        spent = self._spent
        hourly = self.session.scalar(select(func.count()).select_from(AICallRecord).where(
            AICallRecord.created_at >= now - timedelta(hours=1),
            AICallRecord.status.in_(["SUCCEEDED", "FAILED"]),
        )) or 0
        return {
            "spent_today": str(money(spent(_day_start(now)))),
            "spent_month": str(money(spent(_day_start(now).replace(day=1)))),
            "requests_last_hour": hourly,
            "daily_budget": self.settings.ai_daily_budget,
            "monthly_budget": self.settings.ai_monthly_budget,
            "max_requests_per_hour": self.settings.ai_max_requests_per_hour,
            "cost_basis": {
                "input_per_mtok": self.settings.ai_input_cost_per_mtok,
                "output_per_mtok": self.settings.ai_output_cost_per_mtok,
                "note": "Approximate; configure AI_INPUT_COST_PER_MTOK/AI_OUTPUT_COST_PER_MTOK for your model.",
            },
        }

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider.name, "model": self.provider.model,
            "configured": self.provider.configured, "enabled": self.settings.ai_enabled,
            "credential": "GEMINI_API_KEY", **self.usage(),
        }

    def _record(self, task_type: str, request_hash: str, status: str, **values: Any) -> AICallRecord:
        record = AICallRecord(
            provider=self.provider.name, model=self.provider.model, task_type=task_type,
            status=status, request_hash=request_hash, **values,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def _check_budget(self, request: AIRequest, request_hash: str) -> None:
        usage = self.usage()
        estimate = self.cost((len(request.system) + len(request.prompt)) // 4, request.max_output_tokens)
        if decimal(usage["spent_today"]) + estimate > decimal(self.settings.ai_daily_budget):
            self._record(request.task_type, request_hash, "BLOCKED_BUDGET", error="Daily AI budget exhausted")
            raise AIBudgetExceeded("Daily AI budget would be exceeded")
        if decimal(usage["spent_month"]) + estimate > decimal(self.settings.ai_monthly_budget):
            self._record(request.task_type, request_hash, "BLOCKED_BUDGET", error="Monthly AI budget exhausted")
            raise AIBudgetExceeded("Monthly AI budget would be exceeded")
        if usage["requests_last_hour"] >= self.settings.ai_max_requests_per_hour:
            self._record(request.task_type, request_hash, "BLOCKED_RATE", error="Hourly AI request limit reached")
            raise AIBudgetExceeded("Hourly AI request limit reached")

    async def run(self, request: AIRequest) -> AIResult:
        request_hash = hashlib.sha256(f"{request.task_type}\n{request.system}\n{request.prompt}".encode()).hexdigest()
        if not self.settings.ai_enabled or not self.provider.configured:
            self._record(request.task_type, request_hash, "NOT_CONFIGURED", error="AI provider not configured")
            self.session.commit()
            raise AIUnavailable("AI provider is not configured (set GEMINI_API_KEY)")
        try:
            self._check_budget(request, request_hash)
        except AIBudgetExceeded:
            self.session.commit()
            raise
        started = perf_counter()
        attempts = 0
        last_error = ""
        while attempts <= self.settings.ai_max_retries:
            attempts += 1
            try:
                response = await asyncio.wait_for(
                    self.provider.generate(request), timeout=self.settings.ai_timeout_seconds
                )
                record = self._record(
                    request.task_type, request_hash, "SUCCEEDED", input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    estimated_cost=self.cost(response.input_tokens, response.output_tokens),
                    latency_ms=round((perf_counter() - started) * 1000), attempts=attempts,
                )
                self.session.commit()
                return AIResult(response, record)
            except TimeoutError:
                last_error, retryable = "AI request timed out", True
            except AIProviderError as exc:
                last_error, retryable = redact(str(exc), self.settings.gemini_api_key), exc.retryable
            if not retryable or attempts > self.settings.ai_max_retries:
                break
            await self.sleep(min(8.0, 0.5 * 2 ** (attempts - 1)))
        self._record(
            request.task_type, request_hash, "FAILED", latency_ms=round((perf_counter() - started) * 1000),
            attempts=attempts, error=last_error,
        )
        self.session.commit()
        raise AIUnavailable(f"AI call failed after {attempts} attempt(s): {last_error}")
