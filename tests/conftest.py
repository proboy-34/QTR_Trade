import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["DEMO_MODE"] = "false"
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.db import Base
from app.models import Asset, AssetInstrument


@pytest.fixture
def session():
    engine=create_engine("sqlite:///:memory:",connect_args={"check_same_thread":False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine,expire_on_commit=False)() as value:
        asset = Asset(
            base_asset="BTC", quote_asset="USDT", symbol="BTCUSDT", tick_size=.1,
            step_size=.00001, min_quantity=.00001, min_notional=5,
            exchange_mappings={"paper": "BTCUSDT"},
        )
        value.add(asset)
        value.flush()
        value.add(AssetInstrument(
            asset_id=asset.id, exchange="paper", exchange_symbol="BTCUSDT",
            tick_size=.1, step_size=.00001, min_quantity=.00001, min_notional=5,
            price_precision=1, quantity_precision=5, trading_status="TRADING",
        ))
        value.commit()
        yield value
