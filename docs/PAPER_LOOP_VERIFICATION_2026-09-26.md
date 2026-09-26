# REST paper loop: real verification, 2026-09-26

**REAL** means the check hit live providers: Binance `data-api.binance.vision`, Gemini, Finnhub
and FRED. It used a real database and the real scheduler, with no demo mode and no seeded
market data.
**FIXTURE** means a deterministic automated test with fake candles, quotes and AI answers
(`tests/test_candle_cycle.py` and others). Fixture results are never real-market trades.

## Real provider verification (04:18:38 UTC)

| Provider | Endpoint | HTTP | Latency | Result |
| --- | --- | --- | --- | --- |
| Binance public | `/api/v3/ping`, `/time`, `/exchangeInfo`, `/ticker/bookTicker`, `/klines` | 200 | 1.0–1.1 s | HEALTHY (clock skew −14 ms, BTCUSDT TRADING, spread 0.001 bps) |
| Binance account | `/api/v3/account` | — | — | NOT_CONFIGURED: the secret is unavailable. Paper trading does not need it |
| Gemini `gemini-3.8-flash` | model metadata; `:generateContent` | 200 / 200 | 453 ms / 4832 ms | HEALTHY, structured JSON |
| Finnhub | `/news?category=crypto` | 200 | 475 ms | 87 articles |
| Finnhub | `/calendar/economic` | 403 | 456 ms | NOT_AVAILABLE_ON_PLAN. `FINNHUB_CALENDAR_ENABLED=false`; no calendar data is created |
| FRED | `/series/observations` (DGS10), vintage history | 200 | 669 / 799 ms | HEALTHY |

## Real autonomous run (fresh database, real scheduler)

- **Closed-candle cycle, 04:19–04:21 UTC:**
  - Chose the 03:00–04:00 candle; the forming candle was excluded.
  - Synced 32,000 real 1h candles (2,000 per new asset) and scanned 25 assets (4 opportunities).
  - Made 25 decisions, all NO_TRADE: "No active strategy currently satisfies every eligibility and entry condition".
- **Restarts:** after restarting at 04:24 and again at 04:50, the same candle came back `ALREADY_PROCESSED` for all 25 assets. The scan was skipped as already done, 0 rows were re-synced and there were no duplicates.
- **New candle after the restart:** at 05:00:36 UTC, 36 s after the 04:00–05:00 candle closed, the scheduler processed it with nobody acting:
  - synced 25 rows (one per asset), scanned (15 opportunities) and decided 25 assets (NO_TRADE);
  - derived 4h context regimes from closed 1h candles for all 25 assets.
- **Jobs:** all 10 scheduled jobs COMPLETED, with 0 failures in the last 24 h.
- **News:** Finnhub ingestion stored 80 real crypto news items.
- **FRED:**
  - CPIAUCSL, PAYEMS and M2SL were stored with vintages.
  - The daily series DFF, DGS2, DGS10 and T10Y2Y fail with HTTP 400: "5117 vintage dates … exceeds the maximum number of vintage dates allowed (2000)". This is a known, unfixed limitation.
  - UNRATE failed once with an empty error message and succeeded on a later run.

## Real research (on the synced Binance history)

- 19 hypotheses were tested on 2,000 real 1h candles per asset (2026-07-04 to 2026-09-26):
  - 1 came from a scanner signal and 18 from the baseline template programme;
  - the assets were BTC, ETH, XRP, SOL, ZEC, NEAR and SAGA.
- All 19 were rejected:
  - at the in-sample backtest (profit factor below 1.0);
  - out of sample (did not beat passive exposure; the out-of-sample expectancy decayed);
  - at robustness (deflated Sharpe below 0.95; weakest regime profit factor below 0.6).
- Example: BTCUSDT EMA trend change had an in-sample profit factor of 3.78, but a deflated Sharpe of 0.51 against the 0.95 required. It was rejected: one profitable backtest is not enough.
- Result: **0 paper-eligible strategies**, so no paper order was placed. This is the correct outcome, not a failure.

## Real path check (copied database; nothing persisted)

A rejected BTCUSDT research candidate was pushed through the real path on purpose, to prove
each stage works up to where a legitimate trade would be created:

1. The evidence reasoner used the real 03:00 candle (close 83,939): TRADE_PROPOSAL.
2. Gemini, called for real, returned a structured **NO_TRADE** (confidence 0.25). Its reasons: missing 4h context (since fixed by resampling), a negative EMA spread and 24h return, and a low volume ratio.
3. The real Binance quote was bid 83,894.43 / ask 83,894.44.
4. Risk rejected it with `STRATEGY_NOT_PAPER_ELIGIBLE`. 0 orders.

## Known limitations (genuine)

- Binance authenticated account access is unavailable because the secret is not configured. This is not needed for paper trading.
- The Binance WebSocket and `api.binance.com` return HTTP 451 (restricted location) from this environment. The core loop uses REST on `data-api.binance.vision`.
- FRED daily-series vintage ingestion exceeds the 2,000-vintage limit (documented, not fixed).
- The Finnhub economic calendar is not on the current plan.
- No strategy has passed validation yet, so no real-market paper trade has occurred. The fill, P&L and exit mechanics are proven by FIXTURE tests only.
