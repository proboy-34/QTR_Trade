import json
import logging
import re
from datetime import UTC, datetime

# Query-string credentials used by Finnhub (token), FRED (api_key) and others.
QUERY_SECRET = re.compile(r"((?:token|api_key|apikey|key|signature|secret)=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Remove configured credential values and credential-looking query parameters."""
    if secrets is None:
        from app.core.config import get_settings

        secrets = get_settings().secret_values()
    for value in secrets:
        text = text.replace(value, "••••")
    return QUERY_SECRET.sub(r"\1••••", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "event": redact(record.getMessage()),
        }
        for key in ("correlation_id", "strategy_id", "trade_id", "order_id", "position_id", "job"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    # httpx logs full request URLs (including query credentials) at INFO.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
