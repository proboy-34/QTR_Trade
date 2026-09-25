import asyncio

import pytest

from app.core.config import Settings
from app.core.notifications import TelegramNotifier
from app.core.orchestrator import TaskOrchestrator


@pytest.mark.asyncio
async def test_scheduler_executes_jobs_and_observes_failures():
    orchestrator = TaskOrchestrator()
    completed = []

    async def healthy():
        completed.append(True)

    async def failing():
        raise RuntimeError("scheduled failure")

    orchestrator.schedule("healthy", 1, healthy)
    orchestrator.schedule("failing", 1, failing)
    await asyncio.sleep(1.1)
    await orchestrator.stop()
    assert completed
    assert orchestrator.states["healthy"].runs == 1
    assert "scheduled failure" in orchestrator.states["failing"].last_error


@pytest.mark.asyncio
async def test_unconfigured_notification_is_safe_noop():
    assert not await TelegramNotifier(Settings()).send("OrderFilled", "test")

