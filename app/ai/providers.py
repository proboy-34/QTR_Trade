"""Provider-neutral AI interface and the Gemini REST adapter."""

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings


@dataclass
class AIRequest:
    task_type: str
    system: str
    prompt: str
    max_output_tokens: int = 2048
    temperature: float = 0.2
    json_output: bool = True


@dataclass
class AIResponse:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    finish_reason: str | None = None


class AIProviderError(Exception):
    """Provider failure with a retryability flag. Messages never contain credentials."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class AIProvider(Protocol):
    name: str
    model: str

    @property
    def configured(self) -> bool: ...

    async def generate(self, request: AIRequest) -> AIResponse: ...


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "••••")
    return re.sub(r"(key=)[A-Za-z0-9_\-]+", r"\1••••", text)


class GeminiProvider:
    """Google Gemini generateContent over REST. The key travels only in a request header."""

    name = "gemini"

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 30,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport  # injectable for offline tests only

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def body(self, request: AIRequest) -> dict[str, Any]:
        config: dict[str, Any] = {"temperature": request.temperature, "maxOutputTokens": request.max_output_tokens}
        if request.json_output:
            config["responseMimeType"] = "application/json"
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.prompt}]}],
            "generationConfig": config,
        }

    @staticmethod
    def parse(payload: dict[str, Any], model: str) -> AIResponse:
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback", {})
            raise AIProviderError(f"No candidates returned ({feedback.get('blockReason', 'unknown')})", retryable=False)
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts if not part.get("thought"))
        usage = payload.get("usageMetadata") or {}
        return AIResponse(
            text=text,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0)),
            model=str(payload.get("modelVersion") or model),
            finish_reason=candidate.get("finishReason"),
        )

    async def generate(self, request: AIRequest) -> AIResponse:
        if not self.configured:
            raise AIProviderError("GEMINI_API_KEY is not configured", retryable=False)
        url = f"{self.base_url}/models/{self.model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                response = await client.post(url, json=self.body(request), headers={"x-goog-api-key": self._api_key})
        except httpx.TimeoutException as exc:
            raise AIProviderError("Gemini request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(redact(f"Gemini transport error: {type(exc).__name__}", self._api_key), retryable=True) from exc
        if response.status_code >= 400:
            retryable = response.status_code in {408, 429, 500, 502, 503, 504}
            try:
                detail = response.json().get("error", {}).get("status", "")
            except ValueError:
                detail = ""
            raise AIProviderError(
                redact(f"Gemini HTTP {response.status_code} {detail}".strip(), self._api_key),
                retryable=retryable, status_code=response.status_code,
            )
        return self.parse(response.json(), self.model)


class UnconfiguredProvider:
    name = "none"
    model = "none"

    @property
    def configured(self) -> bool:
        return False

    async def generate(self, request: AIRequest) -> AIResponse:
        raise AIProviderError("No AI provider is configured", retryable=False)


def build_provider(settings: Settings) -> AIProvider:
    if settings.ai_enabled and settings.ai_provider == "gemini":
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings.gemini_base_url, settings.ai_timeout_seconds)
    return UnconfiguredProvider()
