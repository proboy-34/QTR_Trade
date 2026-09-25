import asyncio
from dataclasses import asdict, dataclass
from typing import Protocol

from app.core.events import Event, EventBus
from app.core.time import TimeService


class ConnectionProbe(Protocol):
    async def ping(self) -> bool: ...


@dataclass
class ConnectionState:
    provider: str
    status: str = "DISCONNECTED"
    authenticated: bool = False
    rest_healthy: bool = False
    websocket_healthy: bool = False
    reconnect_attempts: int = 0
    last_checked_at: str | None = None
    last_error: str | None = None


class ConnectionManager:
    def __init__(self, event_bus: EventBus, timeout_seconds: float = 10, max_attempts: int = 3) -> None:
        self.event_bus = event_bus
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.states: dict[str, ConnectionState] = {}

    async def connect(
        self, provider: str, probe: ConnectionProbe, *, authenticated: bool = False,
        websocket_healthy: bool = False,
    ) -> ConnectionState:
        state = self.states.setdefault(provider, ConnectionState(provider))
        was_connected = state.status == "CONNECTED"
        for attempt in range(self.max_attempts):
            try:
                state.rest_healthy = bool(
                    await asyncio.wait_for(probe.ping(), timeout=self.timeout_seconds)
                )
                if state.rest_healthy:
                    state.status = "CONNECTED"
                    state.authenticated = authenticated
                    state.websocket_healthy = websocket_healthy
                    state.last_error = None
                    state.last_checked_at = TimeService.now().isoformat()
                    if not was_connected and state.reconnect_attempts:
                        await self.event_bus.publish(Event(
                            "ExchangeReconnected", {"provider": provider}, source="connection_manager"
                        ))
                    return state
                state.reconnect_attempts += 1
                state.last_error = "Probe returned unhealthy"
            except Exception as exc:
                state.reconnect_attempts += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < self.max_attempts:
                await asyncio.sleep(min(2**attempt, 2))
        state.status = "ERROR"
        state.rest_healthy = False
        state.last_checked_at = TimeService.now().isoformat()
        await self.event_bus.publish(Event(
            "ExchangeDisconnected",
            {"provider": provider, "error": state.last_error},
            source="connection_manager",
        ))
        return state

    def snapshot(self) -> dict[str, dict]:
        return {name: asdict(state) for name, state in self.states.items()}
