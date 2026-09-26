# ruff: noqa: E402  (environment must be configured before the app is imported)
import os
import tempfile

# The application-level engine uses a real temporary file database (like production), so
# requests across connections see the same schema. Unit tests use their own in-memory session.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp(prefix='qtr-tests-')}/app.db"
os.environ["DEMO_MODE"] = "false"
# Automated tests are deterministic and offline: real credentials from a local .env must never
# reach them (environment variables take precedence over the .env file).
for _name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET",
              "GEMINI_API_KEY", "FINNHUB_API_KEY", "FRED_API_KEY", "TELEGRAM_BOT_TOKEN", "AUTH_TOKENS"):
    os.environ[_name] = ""
os.environ["EXECUTION_MODE"] = "paper"
# Operator-specific provider settings in a local .env must not change test behaviour either.
os.environ["FINNHUB_NEWS_ENABLED"] = "true"
os.environ["FINNHUB_CALENDAR_ENABLED"] = "true"
os.environ["BINANCE_PUBLIC_BASE_URL"] = "http://127.0.0.1:9"  # tests never reach a real exchange
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
