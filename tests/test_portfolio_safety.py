import hashlib
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from market_fixtures import oscillating, random_walk, store
from paper_fixtures import LEGACY, add_passes, validated_version
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import SafetyError
from app.core.events import EventBus
from app.global_services.live_paper import LivePaperService
from app.models import (
    Asset,
    AssetInstrument,
    Decision,
    ExecutionPlan,
    Order,
    PortfolioSnapshot,
    Position,
    SafetyControl,
    Strategy,
    StrategyVersion,
    TradeIntent,
)
from app.trading.paper_exchange import PaperExchange
from app.trading.pipeline import MarketSnapshot, TradingPipeline
from app.trading.portfolio_intelligence import PortfolioIntelligence
from app.trading.risk import RiskEngine
from app.trading.safety import SafetyMonitor, SafetyService


def instrument(session, symbol: str) -> None:
    asset = Asset(base_asset=symbol[:-4], quote_asset="USDT", symbol=symbol, tick_size=Decimal("0.01"),
                  step_size=Decimal("0.001"), min_quantity=Decimal("0.001"), min_notional=5)
    session.add(asset)
    session.flush()
    session.add(AssetInstrument(asset_id=asset.id, exchange="paper", exchange_symbol=symbol, tick_size=Decimal("0.01"),
                                step_size=Decimal("0.001"), min_quantity=Decimal("0.001"), min_notional=5,
                                price_precision=2, quantity_precision=3, trading_status="TRADING"))
    session.commit()


def portfolio(session, **values) -> None:
    defaults = {"equity": 100_000, "available_balance": 100_000, "exposure": 0, "margin_used": 0, "daily_pnl": 0, "drawdown": 0}
    session.add(PortfolioSnapshot(**{**defaults, **values}))
    session.commit()


def intent(session, symbol: str = "BTCUSDT", strategy_version_id: str | None = None) -> TradeIntent:
    decision = Decision(symbol=symbol, outcome="TRADE", market_context={}, evaluations=[], confidence=1, reasoning=["t"], correlation_id="c")
    session.add(decision)
    session.flush()
    item = TradeIntent(decision_id=decision.id, strategy_version_id=strategy_version_id or validated_version(session, symbol).id,
                       symbol=symbol, side="BUY",
                       entry_price=100, confidence=1)
    session.add(item)
    session.flush()
    return item


def position(session, symbol: str, quantity: float, price: float) -> Position:
    item = Position(strategy_version_id=f"v-{symbol}", symbol=symbol, side="BUY", quantity=quantity, entry_price=price,
                    current_price=price, stop_loss=price * 0.9, take_profit=price * 1.2, status="OPEN")
    session.add(item)
    session.commit()
    return item


def correlated_markets(session) -> None:
    base = random_walk(300, seed=41)
    store(session, base, "BTCUSDT")
    follower = base.copy()
    noise = np.random.default_rng(1).normal(0, 0.0005, len(base))
    follower[["open", "high", "low", "close"]] = follower[["open", "high", "low", "close"]].mul(0.03 * np.exp(noise), axis=0)
    store(session, follower, "ETHUSDT")
    store(session, random_walk(300, seed=99), "XRPUSDT")


def test_portfolio_intelligence_correlation_concentration_and_beta(session):
    correlated_markets(session)
    portfolio(session)
    position(session, "BTCUSDT", 0.1, 60_000)
    position(session, "ETHUSDT", 3, 2_000)
    analysis = PortfolioIntelligence(session).analyze()
    assert analysis["correlation"]["BTCUSDT"]["ETHUSDT"] > 0.9
    assert analysis["betas_to_btc"]["ETHUSDT"] == pytest.approx(1.0, abs=0.1)
    assert analysis["concentration_hhi"] == pytest.approx(0.5) and analysis["effective_bets"] == pytest.approx(2.0)
    assert analysis["gross_exposure"] == pytest.approx(0.12)
    candidate = PortfolioIntelligence(session).assess_candidate("ETHUSDT", 30_000, 100_000, threshold=0.7, max_cluster=0.35, max_symbol=0.5)
    assert "CORRELATED_EXPOSURE_LIMIT" in candidate["reasons"] and "BTCUSDT" in candidate["cluster"]
    independent = PortfolioIntelligence(session).assess_candidate("XRPUSDT", 30_000, 100_000, threshold=0.7, max_cluster=0.35, max_symbol=0.5)
    assert independent["reasons"] == []


def test_risk_sizes_down_for_concentration_and_rejects_exhausted_correlated_budget(session):
    correlated_markets(session)
    instrument(session, "ETHUSDT")
    portfolio(session)
    sized = RiskEngine(session, Settings(max_symbol_concentration=0.1)).assess(intent(session), 100, 0.2)
    assert sized.approved and sized.notional <= Decimal("10000")
    position(session, "BTCUSDT", 0.5, 60_000)  # 30% of equity in BTC
    rejected = RiskEngine(session, Settings(max_correlated_exposure=0.3)).assess(intent(session, "ETHUSDT"), 2_000, 0.2)
    assert not rejected.approved and "CORRELATED_EXPOSURE_LIMIT" in rejected.reason_codes


@pytest.mark.asyncio
async def test_kill_switch_scopes_block_new_trades_but_not_other_assets(session):
    portfolio(session)
    safety = SafetyService(session)
    safety.activate("ASSET", "halt BTC", target="BTCUSDT")
    session.commit()
    assert "SAFETY_ASSET_STOP" in RiskEngine(session, Settings()).assess(intent(session), 100, 0.2).reason_codes
    instrument(session, "SOLUSDT")
    assert RiskEngine(session, Settings()).assess(intent(session, "SOLUSDT"), 100, 0.2).approved
    control, created = safety.activate("NEW_ORDERS", "operator halt")
    duplicate, again = safety.activate("NEW_ORDERS", "operator halt")
    assert created and not again and duplicate.id == control.id
    session.commit()
    blocked = RiskEngine(session, Settings()).assess(intent(session, "SOLUSDT"), 100, 0.2)
    assert not blocked.approved and "SAFETY_NEW_ORDERS_STOP" in blocked.reason_codes
    plan = ExecutionPlan(trade_intent_id=intent(session, "SOLUSDT").id, exchange="paper", symbol="SOLUSDT", side="BUY",
                         quantity=1, risk_amount=1, stop_loss=90, take_profit=110, leverage=1)
    session.add(plan)
    session.flush()
    with pytest.raises(SafetyError, match="halted"):
        await PaperExchange(session, Settings(), EventBus()).submit(plan, session.get(TradeIntent, plan.trade_intent_id), 100, "c")
    assert session.scalar(select(func.count()).select_from(Order)) == 0
    with pytest.raises(ValueError):
        safety.activate("STRATEGY", "needs target")
    safety.clear(control.id, "admin")
    assert "SAFETY_NEW_ORDERS_STOP" not in SafetyService(session).blocks_new_orders(None, "SOLUSDT")


@pytest.mark.asyncio
async def test_system_stop_ignores_evaluation_and_strategy_stop_blocks_only_that_strategy(session):
    portfolio(session)
    strategy = Strategy(name="Blocked", symbol="BTCUSDT", timeframe="1h", status="active")
    session.add(strategy)
    session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version=1, parameters={}, entry_rules=[{"rule": "x"}], exit_rules=[],
                              filters={}, risk_assumptions={}, documentation="d", content_hash=hashlib.sha256(b"b").hexdigest())
    session.add(version)
    session.flush()
    add_passes(session, version.id)
    session.commit()
    SafetyService(session).activate("STRATEGY", "review", target=strategy.id)
    session.commit()
    result = await TradingPipeline(session, Settings(**LEGACY), EventBus()).evaluate(MarketSnapshot("BTCUSDT", 100, 10, .2, 0, "trending", "BULLISH"))
    assert result["risk_outcome"] == "REJECTED" and "SAFETY_STRATEGY_STOP" in result["risk_reasons"]
    SafetyService(session).activate("SYSTEM", "maintenance")
    session.commit()
    halted = await TradingPipeline(session, Settings(**LEGACY), EventBus()).evaluate(MarketSnapshot("BTCUSDT", 100, 10, .2, 0, "trending", "BULLISH"))
    assert halted["outcome"] == "IGNORE" and "SAFETY_SYSTEM_STOP" in halted["reasoning"][0]


@pytest.mark.asyncio
async def test_safety_monitor_triggers_safe_mode_automatically(session):
    portfolio(session, equity=90_000, daily_pnl=-5_000, drawdown=0.2)
    session.add(Position(strategy_version_id="v", symbol="BTCUSDT", side="BUY", quantity=0, entry_price=100, current_price=100,
                         stop_loss=90, take_profit=110, status="OPEN"))
    session.commit()
    stale = (Settings().safety_max_stale_seconds + 60)
    from app.core.time import TimeService
    live = {"running": True, "status": "ERROR", "last_error": "boom",
            "last_message_at": (TimeService.now() - timedelta(seconds=stale)).isoformat()}
    result = await SafetyMonitor(session, Settings(), EventBus()).check(live)
    triggers = {item["trigger"] for item in result["triggered"]}
    assert {"EXCESSIVE_DAILY_LOSS", "EXCESSIVE_DRAWDOWN", "INCONSISTENT_POSITION", "STALE_MARKET_DATA", "PROVIDER_FAILURE"} <= triggers
    again = await SafetyMonitor(session, Settings(), EventBus()).check(live)
    assert again["triggered"] == [] and again["active"] == len(triggers)  # persisted until an operator clears them


@pytest.mark.asyncio
async def test_impossible_price_jump_is_rejected_and_halts_the_asset(session):
    from market_fixtures import candle_payload
    from sqlalchemy.orm import sessionmaker

    frame = oscillating(60)
    store(session, frame.iloc[:-1], "BTCUSDT")
    service = LivePaperService(sessionmaker(bind=session.get_bind(), expire_on_commit=False), Settings(), EventBus())
    payload = candle_payload(frame.iloc[-1], "BTCUSDT")
    payload.update({"close": str(float(payload["close"]) * 3), "high": str(float(payload["close"]) * 3.1)})
    assert not await service.ingest_closed_candle(payload)
    control = session.scalar(select(SafetyControl).where(SafetyControl.scope == "ASSET"))
    assert control.target == "BTCUSDT" and control.trigger == "IMPOSSIBLE_PRICE"


def test_real_money_trading_is_disabled_and_no_live_order_path_exists():
    settings = Settings()
    assert settings.trading_mode == "paper" and not settings.live_trading_enabled
    assert settings.execution_mode == "paper"
    with pytest.raises(ValueError):
        Settings(trading_mode="live")
    with pytest.raises(ValueError):
        Settings(execution_mode="live")  # LIVE is not an offered execution mode
    root = Path(__file__).resolve().parents[1] / "app"
    files = {path.relative_to(root).as_posix(): path.read_text() for path in root.rglob("*.py")}
    testnet, account = "execution/binance_testnet.py", "integrations/binance_account.py"
    allowed = {
        "hmac.new(": {testnet, account},
        "X-MBX-APIKEY": {testnet, account},
        "/api/v3/order": {testnet},
        "/sapi/": {account},
    }
    for token, modules in allowed.items():
        offenders = {name for name, text in files.items() if token in text} - modules
        assert not offenders, f"{token} found outside reviewed modules: {offenders}"
    for forbidden in ("/fapi/", "/api/v5/trade", "/v5/order", "capital/withdraw", "wallet/transfer", "/sapi/v1/asset/transfer"):
        assert not [name for name, text in files.items() if forbidden in text], forbidden
    # The testnet venue's host is a constant for the testnet and cannot point at the real exchange.
    assert 'TESTNET_HOST = "https://testnet.binance.vision"' in files[testnet]
    assert "api.binance.com" not in files[testnet] and "binance_public_base_url" not in files[testnet]
    # Mainnet signed requests are GET-only and allowlisted.
    assert 'READ_ONLY_ENDPOINTS = frozenset({("GET", "/api/v3/account"), ("GET", "/sapi/v1/account/apiRestrictions")})' in files[account]
    assert "client.post" not in files[account] and "client.delete" not in files[account]
    implementations = re.findall(r"class \w+\((?:[^)]*)PrivateExecutionAdapter", "\n".join(files.values()))
    assert implementations == []
