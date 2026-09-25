from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


class TimeService:
    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def ensure_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def convert(value: datetime, timezone: str) -> datetime:
        return TimeService.ensure_utc(value).astimezone(ZoneInfo(timezone))

    @staticmethod
    def candle_boundary(value: datetime, minutes: int) -> datetime:
        value = TimeService.ensure_utc(value)
        minute = (value.minute // minutes) * minutes
        return value.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def next_boundary(value: datetime, minutes: int) -> datetime:
        return TimeService.candle_boundary(value, minutes) + timedelta(minutes=minutes)

