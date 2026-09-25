from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "QTR"
    app_env: Literal["development", "paper", "live"] = "development"
    trading_mode: Literal["paper", "live"] = "paper"
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    database_url: str = "sqlite:///./qtr.db"
    log_level: str = "INFO"
    # Synthetic demo data (seeded candles/strategy/event) is only created when explicitly enabled.
    demo_mode: bool = False
    # Execution venue. LIVE is not offered: no live adapter exists and none may be enabled here.
    execution_mode: Literal["paper", "testnet", "live_disabled"] = "paper"
    default_symbol: str = "BTCUSDT"
    starting_equity: float = 100_000.0
    max_risk_per_trade: float = Field(0.01, gt=0, le=0.05)
    max_total_exposure: float = Field(0.50, gt=0, le=1)
    max_daily_loss: float = Field(0.03, gt=0, le=0.25)
    max_drawdown: float = Field(0.15, gt=0, le=0.50)
    max_open_positions: int = Field(5, ge=1, le=100)
    max_leverage: float = Field(1.0, ge=1, le=10)
    paper_immediate_fill: bool = True
    paper_partial_fill_ratio: float = Field(1.0, gt=0, le=1)
    paper_fee_rate: float = Field(0.0004, ge=0, le=0.02)
    paper_slippage_rate: float = Field(0.0002, ge=0, le=0.02)
    paper_latency_ms: int = Field(0, ge=0, le=60_000)
    market_data_stale_seconds: int = Field(7200, ge=1)
    live_paper_autostart: bool = False
    live_paper_provider: Literal["binance"] = "binance"
    live_paper_symbol: str = "BTCUSDT"
    # Optional comma-separated multi-asset live stream; empty uses LIVE_PAPER_SYMBOL only.
    live_paper_symbols: str = ""
    live_paper_timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h"
    live_paper_bootstrap_candles: int = Field(120, ge=35, le=1000)
    live_paper_max_backoff_seconds: int = Field(30, ge=1, le=300)
    cors_origins: str = "http://localhost:5173,http://localhost:4173"
    paper_spread_bps: float = Field(2.0, ge=0, le=500)

    # Authentication foundation: local_demo grants every role to a local operator.
    # token mode reads AUTH_TOKENS="token:role|role,token2:viewer" (server-side only).
    auth_mode: Literal["local_demo", "token"] = "local_demo"
    auth_tokens: str = ""

    # Multi-asset universe (public exchange metadata; no credentials required)
    universe_provider: Literal["binance"] = "binance"
    binance_public_base_url: str = "https://api.binance.com"
    binance_ws_base_url: str = "wss://stream.binance.com:9443"
    universe_quote_assets: str = "USDT"
    universe_min_quote_volume_24h: float = Field(20_000_000, ge=0)
    universe_max_spread_bps: float = Field(15, gt=0)
    universe_max_abs_change_24h_pct: float = Field(40, gt=0)
    universe_min_history_candles: int = Field(200, ge=35)
    universe_max_assets: int = Field(25, ge=1, le=200)
    universe_exclude_bases: str = "USDC,FDUSD,TUSD,USDP,DAI,BUSD,EUR,TRY,BRL,GBP,AEUR,USDE,PAXG,WBTC,WBETH,BFUSD,XUSD,RLUSD"
    universe_include_symbols: str = ""
    universe_refresh_seconds: int = Field(21_600, ge=300)

    # Market scanner / regime
    market_scanner_enabled: bool = True
    scanner_exchange: str = "binance"
    scanner_timeframes: str = "1h"
    scanner_interval_seconds: int = Field(300, ge=30)
    market_sync_enabled: bool = True
    market_sync_interval_seconds: int = Field(300, ge=30)
    opportunity_ttl_minutes: int = Field(240, ge=5)

    # Market intelligence (news / macro). Empty provider = manual events only.
    # Finnhub: crypto news and (plan permitting) the economic calendar.
    finnhub_api_key: str = ""
    finnhub_news_enabled: bool = True
    finnhub_calendar_enabled: bool = True
    # FRED: point-in-time macro series (ALFRED vintages prevent look-ahead).
    fred_api_key: str = ""
    fred_series: str = "DFF,DGS2,DGS10,T10Y2Y,CPIAUCSL,UNRATE,PAYEMS,M2SL"
    fred_history_years: int = Field(10, ge=1, le=60)
    macro_ingestion_interval_seconds: int = Field(21_600, ge=600)
    provider_check_interval_seconds: int = Field(900, ge=60)
    news_ingestion_interval_seconds: int = Field(900, ge=60)

    # AI research assistant (never an order authority)
    ai_provider: Literal["", "gemini"] = "gemini"
    ai_enabled: bool = True
    gemini_api_key: str = ""
    # Verified 2026-09-25: gemini-2.5-flash is listed but no longer serves new keys.
    gemini_model: str = "gemini-3.8-flash"
    # AI review of TRADE proposals: off, advisory (recorded only) or veto (may turn TRADE into NO_TRADE).
    ai_trade_review: Literal["off", "advisory", "veto"] = "advisory"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ai_timeout_seconds: float = Field(30, gt=0, le=300)
    ai_max_retries: int = Field(2, ge=0, le=5)
    ai_max_output_tokens: int = Field(2048, ge=64, le=65_536)
    ai_daily_budget: float = Field(1.0, ge=0)
    ai_monthly_budget: float = Field(20.0, ge=0)
    ai_max_requests_per_hour: int = Field(30, ge=0)
    ai_input_cost_per_mtok: float = Field(0.30, ge=0)
    ai_output_cost_per_mtok: float = Field(2.50, ge=0)

    # Research loop and learning
    autonomous_research_enabled: bool = True
    research_interval_seconds: int = Field(600, ge=30)
    research_max_hypotheses_per_run: int = Field(3, ge=1, le=50)
    learning_interval_seconds: int = Field(900, ge=60)
    counterfactual_horizon_bars: int = Field(12, ge=1, le=500)

    # Portfolio intelligence and safety
    max_correlated_exposure: float = Field(0.35, gt=0, le=1)
    max_symbol_concentration: float = Field(0.25, gt=0, le=1)
    correlation_threshold: float = Field(0.7, gt=0, le=1)
    safety_monitor_interval_seconds: int = Field(60, ge=10)
    safety_max_stale_seconds: int = Field(900, ge=30)
    safety_max_execution_errors: int = Field(5, ge=1)
    safety_max_price_jump_pct: float = Field(25, gt=0)

    binance_api_key: str = ""
    binance_api_secret: str = ""
    # Spot testnet credentials are separate keys issued by testnet.binance.vision.
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    # Risk: minimum reward/risk and maximum market-data age (in bars) for any new trade.
    min_reward_risk: float = Field(1.5, ge=0.5, le=20)
    market_data_max_age_bars: int = Field(3, ge=1, le=100)
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notification_categories: str = "system,connection,trade,risk,order,position,intelligence,summary"

    @model_validator(mode="after")
    def protect_live_trading(self) -> "Settings":
        if self.trading_mode == "live":
            if not self.live_trading_enabled or self.live_trading_confirmation != "ENABLE_REAL_ORDERS":
                raise ValueError(
                    "Live mode requires LIVE_TRADING_ENABLED=true and "
                    "LIVE_TRADING_CONFIRMATION=ENABLE_REAL_ORDERS"
                )
        if self.app_env == "live" and "*" in self.cors_origins.split(","):
            raise ValueError("Wildcard CORS is forbidden in live environment")
        if self.auth_mode == "token" and not self.auth_tokens.strip():
            raise ValueError("AUTH_MODE=token requires AUTH_TOKENS")
        if self.execution_mode == "testnet" and not (self.binance_testnet_api_key and self.binance_testnet_api_secret):
            raise ValueError("EXECUTION_MODE=testnet requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET")
        if self.live_paper_autostart and self.trading_mode != "paper":
            raise ValueError("Live-data paper mode requires TRADING_MODE=paper")
        return self


    def secret_values(self) -> list[str]:
        """Every configured credential value, for log/error redaction."""
        names = [name for name in type(self).model_fields if name.endswith(("_key", "_secret", "_token", "_passphrase", "_tokens"))]
        return [value for name in names if len(value := str(getattr(self, name) or "")) >= 6]

    def csv(self, name: str) -> list[str]:
        value = getattr(self, name)
        return [item.strip() for item in str(value).split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
