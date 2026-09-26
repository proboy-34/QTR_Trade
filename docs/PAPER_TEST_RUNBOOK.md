# 1-month real-market PAPER test: runbook

QTR runs PAPER ONLY. `EXECUTION_MODE` has no live option (`live` is rejected at startup), no real-money order adapter
exists, and the paper venue refuses to run outside `TRADING_MODE=paper`. An automated test fails if an authenticated
order, withdrawal or transfer endpoint appears in the code. The Binance secret is not needed.

## What runs

The `closed_candle_cycle` job runs every 60 s. For each fully closed 1h candle it:

1. syncs the candle from Binance REST (`data-api.binance.vision`);
2. checks open paper positions for stop, target, strategy exit and maximum holding period, then marks them to the real bid;
3. scans the market;
4. decides once per asset, in this order:
   - paper-eligible strategy;
   - evidence (regime, 4h context, liquidity, Finnhub news, FRED monthly macro);
   - Gemini structured decision;
   - deterministic Risk;
   - a paper fill at the real ask plus slippage and fees.

Each evaluation is journalled with a reason code, including every NO_TRADE. Research runs every 10 minutes and only
promotes strategies that pass backtest, out-of-sample, walk-forward and robustness gates, including regime checks.

**Expect many NO_TRADE decisions, possibly for weeks.** On the verification data every research hypothesis was
rejected. A trade happens only when a strategy legitimately passes validation AND its entry rule, the evidence,
Gemini and Risk all agree.

## Start (Windows, `E:\QTR`)

The simplest way is to double-click `RUN_QTR.bat` in `E:\QTR` (see the README section "One-Click Windows Startup").
It runs the steps below for you, opens the browser, and `STOP_QTR.bat` stops it again. The manual steps are:

```powershell
cd E:\QTR
git fetch origin claude/sweet-hawking-joq2hh
git checkout claude/sweet-hawking-joq2hh
git pull origin claude/sweet-hawking-joq2hh
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Check your local `.env`. Keep your existing API keys; do not commit this file. The values below must be set:

```text
TRADING_MODE=paper
EXECUTION_MODE=paper
LIVE_TRADING_ENABLED=false
DEMO_MODE=false
STARTING_EQUITY=100000
BINANCE_PUBLIC_BASE_URL=https://data-api.binance.vision
FINNHUB_CALENDAR_ENABLED=false
AI_TRADE_REVIEW=required
DATABASE_URL=sqlite:///./qtr_paper_month.db
```

Use a fresh database file for the experiment. The initial capital ($100,000) is recorded in it on first use;
changing `STARTING_EQUITY` later does not change that account.

```powershell
python -m alembic upgrade head          # expect: 0008_paper_account_capital (head)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000   # no --reload for a long run
```

Optionally, run the UI in a second terminal: `cd frontend; npm install; npm run dev`, then open http://localhost:5173.

Checks in the first 5 minutes (`curl` or the browser):

- `http://127.0.0.1:8000/health`:
  - `binance` HEALTHY
  - `binance_account` NOT_CONFIGURED (expected)
  - `market_data` HEALTHY
  - `scheduler` HEALTHY
- `http://127.0.0.1:8000/api/v1/paper/account`:
  - `initial_balance` 100000
  - `execution_mode` PAPER
  - `live_trading` DISABLED
- `http://127.0.0.1:8000/api/v1/candle-cycle/status`: rows appear a minute or two after startup.

Keep the machine awake and the process running. The scheduler only runs while the app runs. After downtime:

- open positions are checked against every candle that closed while the app was down;
- entry decisions are made only for the latest closed candle, because stale candles are not replayed as decisions.

## Kill switches (stop new trades; history and open positions are kept)

```text
POST /api/v1/safety/controls  {"scope": "EMERGENCY", "reason": "..."}                 # stop all new trades
POST /api/v1/safety/controls  {"scope": "ASSET", "target": "SOLUSDT", "reason": "..."}
POST /api/v1/safety/controls  {"scope": "STRATEGY", "target": "<strategy id>", "reason": "..."}
POST /api/v1/safety/controls/{id}/clear                                                # operator clears it
```

Protective exits (stop, target, strategy exit) keep running under every stop.

## Collect during the month

Export these daily (or at least weekly):

| Endpoint | What it holds |
| --- | --- |
| `/api/v1/decisions/journal?limit=5000` | Every evaluation: final decision, reason code, market state, strategies, Gemini and Risk results |
| `/api/v1/paper/account` | Balance, cash, reserved capital, P&L, fees, slippage, return, drawdown, equity curve |
| `/api/v1/paper/trades?limit=500` plus `/api/v1/paper/trades/{id}` | The full audit record of each trade |
| `/api/v1/strategies/lifecycle` | Candidates, paper-eligible and rejected strategies, with reasons |
| `/api/v1/system/jobs/registry` and `/api/v1/candle-cycle/status?limit=500` | Job health, failures, processed candles |
| `/api/v1/system/provider-checks` | Provider availability over time (Gemini 503/429 frequency) |
| the server log (stdout, JSON lines) | Contains no credentials |
| the SQLite database file | The complete record. Back it up weekly; copy it only while the app is stopped, or use SQLite's backup tool |

Questions to answer at the end:

- How many candles were processed, and how many were missed (downtime)?
- What was the distribution of NO_TRADE reason codes?
- How often was Gemini unavailable?
- How many strategies reached paper eligibility?
- Trade count, net P&L after fees and slippage, max drawdown, and paper-versus-backtest expectancy.

## Known limitations

- Stop-loss and take-profit are checked every 60 s through REST (real bid), plus candle-by-candle OHLC. This is
  **not tick-by-tick**. When one hourly candle touches both, the stop is assumed first. Gaps fill at the open.
- Gemini (`gemini-3.8-flash`) intermittently returns 503 (high demand) or 429 (quota). With
  `AI_TRADE_REVIEW=required` that means NO_TRADE for that decision, never a trade.
- The Binance WebSocket, `api.binance.com` and the Binance testnet may return HTTP 451 from restricted locations.
  The core loop does not use them.
- Binance authenticated account access is not configured (no secret). Paper trading does not need it.
- The Finnhub economic calendar is not on the plan (403); it is disabled and never fabricated.
- The FRED daily series (DFF, DGS2, DGS10, T10Y2Y) exceed FRED's 2,000-vintage limit; the monthly series work.
- Only 1h candles are traded (`SCANNER_TIMEFRAMES=1h`). 4h context is derived from closed 1h candles.
