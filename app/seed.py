import hashlib
import math
import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.time import TimeService
from app.models import (
    Asset,
    AssetInstrument,
    Dataset,
    IntegrationMetadata,
    MarketCandle,
    MarketEvent,
    PortfolioSnapshot,
    Strategy,
    StrategyVersion,
    SystemEvent,
    ValidationResult,
)


def seed_demo(session: Session, settings: Settings) -> None:
    """Synthetic candles, a demo strategy with seeded (not researched) evidence and a fabricated
    event. Only ever called with DEMO_MODE=true; every row is flagged as demo."""
    if not settings.demo_mode:
        raise RuntimeError("seed_demo requires DEMO_MODE=true")
    asset = session.scalar(select(Asset).where(Asset.symbol == "BTCUSDT"))
    if not asset:
        asset = Asset(
            base_asset="BTC", quote_asset="USDT", symbol="BTCUSDT", tick_size=0.1,
            step_size=0.00001, min_quantity=0.00001, min_notional=5,
            exchange_mappings={"paper": "BTCUSDT", "binance":"BTCUSDT", "okx":"BTC-USDT", "bybit":"BTCUSDT"},
        )
        session.add(asset)
        session.flush()
    existing_exchanges = set(session.scalars(select(AssetInstrument.exchange).where(
        AssetInstrument.asset_id == asset.id
    )).all())
    symbols = {"paper": "BTCUSDT", "binance": "BTCUSDT", "okx": "BTC-USDT", "bybit": "BTCUSDT"}
    for exchange, exchange_symbol in symbols.items():
        if exchange not in existing_exchanges:
            session.add(AssetInstrument(
                asset_id=asset.id, exchange=exchange, exchange_symbol=exchange_symbol,
                contract_type="spot", tick_size=0.1, step_size=0.00001,
                min_quantity=0.00001, min_notional=5,
                price_precision=1, quantity_precision=5, trading_status="TRADING",
                metadata_payload={"demo": True},
            ))
    session.commit()
    if session.scalar(select(func.count(Strategy.id))):
        return
    now = TimeService.now()
    rng = random.Random(42)
    price = 62_000.0
    candles = []
    for index in range(240):
        drift = 34 + math.sin(index / 9) * 115 + rng.gauss(0, 95)
        open_price = price
        price = max(1, price + drift)
        high = max(open_price, price) + abs(rng.gauss(70, 35))
        low = min(open_price, price) - abs(rng.gauss(70, 35))
        candles.append(MarketCandle(
            exchange="paper", symbol="BTCUSDT", timeframe="1h",
            timestamp=now - timedelta(hours=239-index), open=open_price, high=high, low=low,
            close=price, volume=900 + rng.random() * 1000, funding_rate=0.0001,
            open_interest=12_400_000_000 + index * 900_000, is_demo=True,
        ))
    session.add_all(candles)
    strategy = Strategy(
        name="BTC EMA Momentum", description="Deterministic EMA crossover with regime filter.",
        symbol="BTCUSDT", timeframe="1h", status="active", is_demo=True,
    )
    session.add(strategy)
    session.flush()
    version = StrategyVersion(
        strategy_id=strategy.id, version=1, parameters={"fast": 10, "slow": 30},
        entry_rules=[{"left": "ema_fast", "operator": "crosses_above", "right": "ema_slow"}],
        exit_rules=[{"left": "ema_fast", "operator": "crosses_below", "right": "ema_slow"}],
        filters={"regime": ["trending", "bullish"]},
        risk_assumptions={"risk_fraction": 0.01, "stop_loss_pct": 0.02},
        documentation="Demo strategy. Signals execute on the next candle to prevent look-ahead bias.",
        content_hash=hashlib.sha256(b"btc-ema-momentum-v1").hexdigest(),
    )
    session.add(version)
    session.flush()
    session.add(ValidationResult(
        strategy_version_id=version.id, method="walk_forward", result="PASS",
        metrics={"out_of_sample_return": 8.4, "max_drawdown": -4.8, "profit_factor": 1.61},
        rules={"min_profit_factor": 1.2, "max_drawdown": -15}, notes="Seeded demo evidence",
    ))
    session.add_all([
        Dataset(name="BTCUSDT demo hourly", symbol="BTCUSDT", timeframe="1h", source="synthetic", row_count=240, is_demo=True),
        PortfolioSnapshot(equity=settings.starting_equity, available_balance=settings.starting_equity, exposure=0, margin_used=0, daily_pnl=0, drawdown=0),
        MarketEvent(category="MACRO", severity="HIGH", event_at=now + timedelta(days=2), source="demo", affected_assets=["BTC","ETH"], description="DEMO DATA — fabricated FOMC event for demo mode only", title="DEMO: FOMC rate decision", status="scheduled", provider="demo", verification_status="DEMO", processing_status="DEMO", available_at=now),
        SystemEvent(type="ApplicationStarted", component="lifecycle", severity="INFO", message="QTR demo initialized", payload={"demo": True}, correlation_id="demo-startup"),
    ])
    configured = {
        "binance": bool(settings.binance_api_key and settings.binance_api_secret),
        "okx": bool(settings.okx_api_key and settings.okx_api_secret and settings.okx_passphrase),
        "bybit": bool(settings.bybit_api_key and settings.bybit_api_secret),
        "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
    }
    session.add_all([
        IntegrationMetadata(provider=name, configured=value, connection_status="configured" if value else "not_configured")
        for name, value in configured.items()
    ])
    session.commit()
