# Trading safety

- `EXECUTION_MODE` is one of `paper` (default), `testnet` or `live_disabled`. `live` is rejected at startup; no real-money order adapter exists. In `live_disabled` there is no venue and Risk rejects every intent with `EXECUTION_DISABLED`.
- `testnet` routes approved plans to the Binance **Spot Testnet** (`app/execution/binance_testnet.py`). Its host is a code constant (`https://testnet.binance.vision`), it requires its own `BINANCE_TESTNET_API_KEY/SECRET` (never the mainnet key), and every order, fill, position and portfolio snapshot is stored with `venue="testnet"`, so paper, testnet and (future) live state never mix.
- A configuration declaring `TRADING_MODE=live` is rejected unless both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_CONFIRMATION=ENABLE_REAL_ORDERS` are present; those gates express intent only. An automated allowlist test fails if signed requests appear outside the testnet adapter and the read-only mainnet account check, if `/api/v3/order` appears outside the testnet adapter, or if any futures, withdrawal or transfer endpoint appears.
- The mainnet Binance key is used only for read-only verification (`GET /api/v3/account`, `GET /sapi/v1/account/apiRestrictions`). A key with withdrawals or internal transfers enabled is reported as `UNSAFE`. Having a key never enables execution.
- Before a trade proposal reaches Risk, the evidence reasoner (`app/trading/reasoning.py`) records supporting, contradicting and blocking evidence that was knowable at decision time. Blocking evidence, or more contradicting than supporting evidence, produces NO TRADE. AI trade review (`AI_TRADE_REVIEW=off|advisory|veto`) can only turn a TRADE into NO TRADE.
- Paper eligibility is checked independently by Decision and by Risk (`app/trading/eligibility.py`): repository status `paper_testing`/`approved`/`active` AND PASS evidence for backtest, out-of-sample, walk-forward and robustness (incl. regime coverage) on the latest version. One profitable backtest is never enough.
- With `AI_TRADE_REVIEW=required` (default) a paper order needs a structured Gemini TRADE decision with valid levels (stop < entry < target, LONG only). Gemini failure, a malformed answer or NO_TRADE produces NO_TRADE; a Gemini TRADE still has to pass every Risk gate.
- Paper fills require a real Binance bid/ask (`QUOTE_UNAVAILABLE`, `STALE_QUOTE`, `SPREAD_TOO_WIDE`); provider failures reported by the loop reject (`PROVIDER_FAILURE:*`); data newer than the decision time rejects (`LOOKAHEAD_VIOLATION`); final size and risk are re-checked (`POSITION_TOO_LARGE`, `RISK_PER_TRADE_EXCEEDED`, `INVALID_STOP_OR_TARGET`).
- Research can create candidates only. `approved` and `active` transitions require an operator (`actor="operator"`) and PASS validation evidence; research attempts raise a safety error.
- AI is a researcher: it cannot create market facts, decisions, intents or orders. AI strategies are declarative data validated by QTR; no generated code is executed.
- Active and paper-testing strategy versions cannot be edited. New work becomes a new version after suspension.
- Trade Intent contains no final stop, target, size, or leverage. Risk owns all final plan values.
- Risk rejects zero capital, insufficient margin, excessive daily loss/drawdown/exposure, maximum positions, invalid market inputs, exchange minimum violations, exhausted symbol/correlated-cluster budgets, and any active kill switch — before an Execution Plan exists. With market facts it also rejects stale or missing candles (`STALE_MARKET_DATA`, `NO_MARKET_DATA`), missing/failed liquidity and spread evidence, and reward:risk below `MIN_REWARD_RISK`.

## Kill switch and safe mode

| Scope | Effect |
| --- | --- |
| `EMERGENCY` | Like `SYSTEM` (global stop); intended for an operator emergency stop |
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
