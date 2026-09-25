# Trading safety

- Paper trading is the default and the only executable adapter. `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` are the checked-in defaults.
- A configuration declaring `TRADING_MODE=live` is rejected unless both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_CONFIRMATION=ENABLE_REAL_ORDERS` are present. Those gates express intent only: no private live order route, signed request, withdrawal or transfer code exists. An automated test scans the application source for authenticated order/withdrawal endpoints and fails if any appears.
- Binance public market data needs no credentials. Private Binance keys are never used for paper execution.
- Research can create candidates only. `approved` and `active` transitions require an operator (`actor="operator"`) and PASS validation evidence; research attempts raise a safety error.
- AI is a researcher: it cannot create market facts, decisions, intents or orders. AI strategies are declarative data validated by QTR; no generated code is executed.
- Active and paper-testing strategy versions cannot be edited. New work becomes a new version after suspension.
- Trade Intent contains no final stop, target, size, or leverage. Risk owns all final plan values.
- Risk rejects zero capital, insufficient margin, excessive daily loss/drawdown/exposure, maximum positions, invalid market inputs, exchange minimum violations, exhausted symbol/correlated-cluster budgets, and any active kill switch — before an Execution Plan exists.

## Kill switch and safe mode

| Scope | Effect |
| --- | --- |
| `SYSTEM` | Decisions become IGNORE; scanning, sync and research jobs pause; no new orders |
| `PAPER_TRADING` | Decisions become IGNORE; no new paper orders |
| `NEW_ORDERS` | Risk rejects every new intent; the paper venue also refuses |
| `STRATEGY:<id>` | Risk rejects intents from that strategy |
| `ASSET:<symbol>` | Risk rejects intents for that symbol |

Protective exits (stop-loss / take-profit / strategy exits) keep running under every stop — reducing risk is always allowed. Automatic triggers (daily loss, drawdown, stale live data, provider error, repeated rejected orders, inconsistent positions, non-positive equity, impossible price jumps) create persisted controls that stay active until an operator clears them (`POST /api/v1/safety/controls/{id}/clear`, admin role).

## Other safeguards

- Paper orders use stable client order IDs derived from execution plan IDs; fills use unique execution keys.
- Exchange reconciliation and recovery report discrepancies; they never make corrective trades.
- Financial trade/account values use fixed-precision Decimal/NUMERIC handling.
- Secrets remain server-side; logs, status endpoints and AI errors never contain secret values (the Gemini key travels only in a request header and is redacted from errors).
- Live environments reject wildcard CORS. `AUTH_MODE=token` provides role-based access (viewer, researcher, trader, admin); use an external identity provider before any internet exposure.
