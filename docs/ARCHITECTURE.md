# QTR implementation architecture

QTR is one FastAPI application and one React client backed by one relational database. Conceptual services remain separate Python modules and do not imply network services.

```text
Platform Core
  ├─ configuration · lifecycle · time · logging · health · scheduler · secrets · auth roles · lineage
  └─ in-process typed Event Bus (persisted audit events)
         │
Global Services
  ├─ historical adapters + reconnecting Binance live stream (single or combined multi-asset)
  ├─ universe & eligibility (dynamic exchange metadata) · market scanner · regime engine
  ├─ market intelligence (Finnhub news/calendar, structured, source-verified)
  ├─ integrations: provider verification (Binance · Gemini · Finnhub · FRED) · FRED point-in-time macro
  ├─ features · validation/backtesting · assets · time
  └─ Strategy Repository  ← only Research/Trading bridge
         │
   ┌─────┴─────────────────────────────┐
Research                               Trading
hypotheses (DSL specs) → experiments   observation → context → eligibility → decision
→ IS/OOS/walk-forward/robustness       (declarative entry rules on closed candles)
→ candidate → paper validation                     ↓
→ repository (paper_testing →          trade intent → portfolio & risk (+ correlation,
  ready_for_review → human)            concentration, kill switch) → execution plan
AI researcher (budget + firewall)                  ↓
                                        paper exchange → order → fill → position
                                                   ↓
Memory & Learning ◄───────────────────── trade memory · decision lineage · market memory
post-trade analysis · counterfactuals · strategy health · discrepancy · knowledge base
                         └──► new hypotheses (never direct rule changes)
```

Research code writes candidate strategies and immutable versions to the repository; it cannot approve or activate them. Decision reads `active` and `paper_testing` repository versions (all execution is paper), persists market context, lineage and opportunities, and emits a directional Trade Intent. It never creates strategies or calculates final size, leverage, stop, or target. Portfolio/Risk can reject the intent (including kill-switch, symbol-concentration and correlated-cluster limits) and is the only layer that creates final values in an immutable Execution Plan; it honours a strategy's declared stop/target within hard bounds. Execution consumes that plan unchanged and re-checks the kill switch.

The live-paper coordinator bootstraps public Binance history, consumes validated closed WebSocket candles (one symbol or a combined multi-asset stream), updates the regime history, runs Decision only for a matching strategy timeframe, applies the strategy's declarative exit rule on closed candles, and monitors stored stops/targets from live prices. Exits fill with slippage and half-spread against the position; gaps fill at the observed price. Impossible price jumps are rejected and halt the asset.

## Decision evidence, venues and truthful health (migration `0006`)

- `app/trading/reasoning.py` turns a strategy setup into a structured rationale before Risk: timeframe roles (higher timeframe = context and may block; setup timeframe = regime/setup/entry; Risk = size/stop/target; exits = strategy rule + stop/target), supporting / contradicting / blocking evidence, invalidation, horizon, macro context (FRED as-of), news context, historical analogues, data sources and the risk-configuration fingerprint. Only evidence knowable at the decision time is used (`knowable_at` = `available_at`, else `published_at`, else `event_at`). The rationale is stored on `Decision.rationale`, together with the Risk outcome and the approved plan.
- Execution venues: `app/trading/accounts.py` maps `EXECUTION_MODE` to a venue (`paper`, `testnet`, or `none` for `live_disabled`). Positions and portfolio snapshots carry `venue`; Risk, safety and portfolio intelligence are venue-scoped. `app/execution/binance_testnet.py` is the only signed order path and is pinned to the testnet host.
- `app/integrations/health.py` performs real provider checks and stores each as a `ProviderCheck` row (endpoint, result, HTTP status, latency, redacted detail). `/health`, `/api/v1/system/providers` and `/api/v1/integrations` derive state only from these rows, data freshness, jobs and the database: HEALTHY / DEGRADED / UNAVAILABLE / STALE / NOT_CONFIGURED / NOT_VERIFIED.
- `app/integrations/fred.py` stores FRED/ALFRED vintages in `macro_observations` (observation date, `realtime_start/end`, `available_at`, `retrieved_at`); `macro_as_of(t)` returns only values whose vintage was public at `t`.

The scheduler (`app/jobs.py`) runs universe refresh, market-data sync, scanning, news ingestion (Finnhub), macro ingestion (FRED), provider verification, testnet reconciliation (testnet mode only), the research queue, learning, data-integrity checks, opportunity cleanup and the safety monitor. Placeholder jobs that reported success without doing work were removed; every job has a stated purpose, schedule, retry policy and persisted duration/outcome (`GET /api/v1/system/jobs/registry`). Each run is persisted as a `JobExecution`; runs interrupted by a restart are marked `INTERRUPTED`; a SYSTEM stop pauses scanning and research.

The relational schema adds (migration `0005`): asset eligibility runs, regime history, AI calls and artifacts, trade memory, post-trade analyses, knowledge entries, counterfactuals, strategy health, discrepancy reports and safety controls; and extends hypotheses (lifecycle, spec, evidence), strategies (origin, champion link, health), market events (provider identity, verification, macro values), decisions (lineage), market contexts (features, regime) and opportunities (scanner signals, ranking).

SQLite is the developer/demo default. PostgreSQL is the container and production target. Financial persistence uses purpose-specific fixed-precision `NUMERIC` types and business calculations use `Decimal`; scientific indicators and research statistics use floating point.

See [AUTONOMOUS_TRADING_ROADMAP.md](AUTONOMOUS_TRADING_ROADMAP.md) for the capability map and [RESEARCH_AND_LEARNING.md](RESEARCH_AND_LEARNING.md) for validation gates and memory.
