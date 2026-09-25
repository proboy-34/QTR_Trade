import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.time import TimeService

EventHandler = Callable[["Event"], Awaitable[None]]


@dataclass(slots=True)
class Event:
    type: str
    payload: dict[str, Any]
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime = field(default_factory=TimeService.now)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    source: str = "qtr"
    version: int = 1


class EventBus:
    """Typed-by-name in-process event bus with observable delivery statistics."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self.published = 0
        self.failures = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        self.published += 1
        results = await asyncio.gather(
            *(handler(event) for handler in self._handlers[event.type]), return_exceptions=True
        )
        self.failures += sum(isinstance(result, Exception) for result in results)

    @property
    def healthy(self) -> bool:
        return self.failures == 0


async def publish_persisted(session: Any, bus: EventBus, event: Event, component: str) -> None:
    """Persist the audit event in the caller's transaction and notify subscribers."""
    from sqlalchemy import select

    from app.models import SystemEvent

    if session.scalar(select(SystemEvent.id).where(SystemEvent.event_id == event.event_id)):
        return
    session.add(SystemEvent(
        event_id=event.event_id,
        type=event.type,
        component=component,
        severity="INFO",
        message=event.type,
        payload=event.payload,
        source=event.source,
        version=event.version,
        correlation_id=event.correlation_id,
        occurred_at=event.occurred_at,
    ))
    await bus.publish(event)
