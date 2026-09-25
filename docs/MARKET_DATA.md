# Market data and backfills

Historical ingestion is provider-neutral. Binance, OKX, Bybit, and the offline deterministic `paper` provider implement the same bounded-batch interface. Backfill jobs persist their range, next timestamp, last successful timestamp, row count, retries, progress, failure reason, and status.

Create a job with `POST /api/v1/market-data/backfills`, inspect it with the list/detail endpoints, and run or resume it with `POST /api/v1/market-data/backfills/{id}/resume`. Each batch is validated and committed before the next is fetched, so a stopped process resumes from its checkpoint rather than loading or repeating the entire range.

Validation rejects missing fields, duplicate/out-of-order timestamps, invalid or impossible OHLC, negative volume, future timestamps, stale feeds, and exchange timestamp misalignment. Gaps are reported. Failed records are inspectable at `GET /api/v1/market-data/validation-failures`; they are never silently added to a dataset.

Public provider calls are subject to upstream availability and limits. Deterministic tests use fake or paper providers and never rely on exchange availability.

The Binance live adapter subscribes to public kline updates. Current prices are used to monitor existing paper stops/targets; only a closed candle is persisted and sent through Decision. Feed state is available from `/api/v1/live-paper/status`, and session history from `/api/v1/live-paper/sessions`. Automatic warm-up and WebSocket reconnects are bounded and observable.

Market queries accept `exchange` and `timeframe` filters so Binance live history cannot silently mix with the synthetic demo feed.

## Multi-asset universe, sync and scanning

The asset universe is discovered, not hard-coded. `universe_refresh` reads Binance public `exchangeInfo`, 24h tickers and book tickers, evaluates every instrument quoted in `UNIVERSE_QUOTE_ASSETS`, and persists an immutable eligibility verdict per instrument with its reasons (`NOT_TRADING`, `ILLIQUID`, `SPREAD_TOO_WIDE`, `ABNORMAL_MARKET_MOVE`, `EXCLUDED_BASE_ASSET`, `LEVERAGED_TOKEN`, `RECENT_DATA_QUALITY_FAILURES`, `OUTSIDE_MAX_UNIVERSE_SIZE`, …). Eligible instruments are ranked by liquidity and capped at `UNIVERSE_MAX_ASSETS`; their tick size, step size and minimums are mirrored onto the paper venue so simulated orders face real constraints. Inspect it at `GET /api/v1/universe`.

`market_data_sync` incrementally backfills closed candles for eligible symbols from the last stored candle (or `UNIVERSE_MIN_HISTORY_CANDLES` + 100 bars for new assets). Every batch passes the same validation and deduplication as other backfills.

`market_scan` examines eligible assets on `SCANNER_TIMEFRAMES` from stored closed candles, updates regime and market memory, and creates or refreshes deduplicated scanner opportunities with signals (volume spike, volatility expansion, breakouts, abnormal move, trend/momentum change, liquidity drop, correlation change, market-wide move, regime transition), linked news, and an explainable rank. The scanner never trades.

For real-time multi-asset paper trading, set `LIVE_PAPER_SYMBOLS=BTCUSDT,ETHUSDT,...` or pass `symbols` to `POST /api/v1/live-paper/start`; one combined Binance kline stream is used. A closed candle whose close moves more than `SAFETY_MAX_PRICE_JUMP_PCT` from the previous close is rejected, recorded as a validation failure, and halts that asset.

Timestamps read back from SQLite are normalized to UTC before provider requests, so resumed backfills request the correct windows on any host timezone.
