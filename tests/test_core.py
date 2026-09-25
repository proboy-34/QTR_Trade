from datetime import datetime

import pytest

from app.core.config import Settings
from app.core.events import Event, EventBus
from app.core.time import TimeService


def test_live_trading_requires_two_gates():
    with pytest.raises(ValueError): Settings(trading_mode="live",live_trading_enabled=True)
    value=Settings(trading_mode="live",live_trading_enabled=True,live_trading_confirmation="ENABLE_REAL_ORDERS")
    assert value.trading_mode == "live"


def test_live_environment_rejects_wildcard_cors():
    with pytest.raises(ValueError):
        Settings(
            app_env="live",
            trading_mode="live",
            live_trading_enabled=True,
            live_trading_confirmation="ENABLE_REAL_ORDERS",
            cors_origins="*",
        )

def test_time_service_uses_utc_and_boundaries():
    value=TimeService.candle_boundary(datetime(2025,1,1,12,37),15)
    assert value.minute == 30 and value.tzinfo is not None

@pytest.mark.asyncio
async def test_event_bus_delivers_typed_event():
    seen=[]; bus=EventBus()
    async def handler(event): seen.append(event.type)
    bus.subscribe("OrderFilled",handler); await bus.publish(Event("OrderFilled",{"id":"1"}))
    assert seen == ["OrderFilled"] and bus.healthy
