# Market data and backfills

Historical ingestion is provider-neutral. Binance, OKX, Bybit, and the offline deterministic `paper` provider implement the same bounded-batch interface. Backfill jobs persist their range, next timestamp, last successful timestamp, row count, retries, progress, failure reason, and status.

Create a job with `POST /api/v1/market-data/backfills`, inspect it with the list/detail endpoints, and run or resume it with `POST /api/v1/market-data/backfills/{id}/resume`. Each batch is validated and committed before the next is fetched, so a stopped process resumes from its checkpoint rather than loading or repeating the entire range.

Validation rejects missing fields, duplicate/out-of-order timestamps, invalid or impossible OHLC, negative volume, future timestamps, stale feeds, and exchange timestamp misalignment. Gaps are reported. Failed records are inspectable at `GET /api/v1/market-data/validation-failures`; they are never silently added to a dataset.

Public provider calls are subject to upstream availability and limits. Deterministic tests use fake or paper providers and never rely on exchange availability.

The Binance live adapter subscribes to public kline updates. Current prices are used to monitor existing paper stops/targets; only a closed candle is persisted and sent through Decision. Feed state is available from `/api/v1/live-paper/status`, and session history from `/api/v1/live-paper/sessions`. Automatic warm-up and WebSocket reconnects are bounded and observable.

Market queries accept `exchange` and `timeframe` filters so Binance live history cannot silently mix with the synthetic demo feed.
