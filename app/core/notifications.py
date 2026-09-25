import logging

import httpx

from app.core.config import Settings

logger = logging.getLogger("qtr.notifications")


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, event: str, message: str) -> bool:
        if not self.configured:
            logger.info("notification_skipped_unconfigured", extra={"event_type": event})
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={"chat_id": self.chat_id, "text": f"QTR · {event}\n{message}"},
            )
            response.raise_for_status()
        return True

