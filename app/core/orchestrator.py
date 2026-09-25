import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.time import TimeService

logger = logging.getLogger("qtr.orchestrator")
Job = Callable[[], Awaitable[None]]


@dataclass
class JobState:
    name: str
    interval_seconds: int
    last_run: datetime | None = None
    last_error: str | None = None
    runs: int = 0
    enabled: bool = True
    status: str = "IDLE"
    next_run: datetime | None = None
    skipped_overlaps: int = 0
    running: bool = False


class TaskOrchestrator:
    """Observable in-process scheduler for safe recurring and on-demand jobs."""

    def __init__(self) -> None:
        self.states: dict[str, JobState] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    def schedule(self, name: str, interval_seconds: int, job: Job, initial_delay: float | None = None) -> None:
        if name in self.states:
            raise ValueError(f"Job already registered: {name}")
        first = interval_seconds if initial_delay is None else initial_delay
        self.states[name] = JobState(name, interval_seconds)
        self.states[name].next_run = TimeService.now() + timedelta(seconds=first)
        self._tasks.append(asyncio.create_task(self._run(self.states[name], job, first)))

    async def _run(self, state: JobState, job: Job, first_delay: float | None = None) -> None:
        delay = state.interval_seconds if first_delay is None else first_delay
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                delay = state.interval_seconds
                if not state.enabled:
                    state.status = "DISABLED"
                    continue
                if state.running:
                    state.skipped_overlaps += 1
                    continue
                try:
                    state.running = True
                    state.status = "RUNNING"
                    await job()
                    state.last_run = TimeService.now()
                    state.runs += 1
                    state.last_error = None
                    state.status = "HEALTHY"
                except Exception as exc:  # scheduler boundary records and continues
                    state.last_error = f"{type(exc).__name__}: {exc}"
                    logger.exception("scheduled_job_failed", extra={"job": state.name})
                    state.status = "ERROR"
                finally:
                    state.running = False
                    state.next_run = TimeService.now() + timedelta(seconds=state.interval_seconds)

    async def stop(self) -> None:
        self._stopping.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)
