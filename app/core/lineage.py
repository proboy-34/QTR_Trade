"""Data lineage helpers so any decision can be reconstructed months later."""

import hashlib
import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import Settings


@lru_cache
def code_version() -> str:
    """Git commit of the running code, or QTR_CODE_VERSION when deployed without .git."""
    configured = os.getenv("QTR_CODE_VERSION")
    if configured:
        return configured
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=2,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def risk_configuration(settings: Settings) -> dict[str, Any]:
    return {
        "max_risk_per_trade": settings.max_risk_per_trade,
        "max_total_exposure": settings.max_total_exposure,
        "max_daily_loss": settings.max_daily_loss,
        "max_drawdown": settings.max_drawdown,
        "max_open_positions": settings.max_open_positions,
        "max_leverage": settings.max_leverage,
        "max_correlated_exposure": settings.max_correlated_exposure,
        "max_symbol_concentration": settings.max_symbol_concentration,
        "paper_fee_rate": settings.paper_fee_rate,
        "paper_slippage_rate": settings.paper_slippage_rate,
        "paper_spread_bps": settings.paper_spread_bps,
    }


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
