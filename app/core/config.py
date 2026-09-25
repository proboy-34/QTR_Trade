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
    demo_mode: bool = True
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
    live_paper_timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1h"
    live_paper_bootstrap_candles: int = Field(120, ge=35, le=1000)
    live_paper_max_backoff_seconds: int = Field(30, ge=1, le=300)
    cors_origins: str = "http://localhost:5173,http://localhost:4173"

    binance_api_key: str = ""
    binance_api_secret: str = ""
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
        if self.live_paper_autostart and self.trading_mode != "paper":
            raise ValueError("Live-data paper mode requires TRADING_MODE=paper")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
