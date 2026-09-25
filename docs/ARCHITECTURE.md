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
  ├─ market intelligence (provider-neutral news/macro, structured, source-verified)
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

The scheduler (`app/jobs.py`) runs universe refresh, market-data sync, scanning, news/macro ingestion, the research queue, learning, data-integrity checks, opportunity cleanup and the safety monitor. Each run is persisted as a `JobExecution`; runs interrupted by a restart are marked `INTERRUPTED`; a SYSTEM stop pauses scanning and research.

The relational schema adds (migration `0005`): asset eligibility runs, regime history, AI calls and artifacts, trade memory, post-trade analyses, knowledge entries, counterfactuals, strategy health, discrepancy reports and safety controls; and extends hypotheses (lifecycle, spec, evidence), strategies (origin, champion link, health), market events (provider identity, verification, macro values), decisions (lineage), market contexts (features, regime) and opportunities (scanner signals, ranking).

SQLite is the developer/demo default. PostgreSQL is the container and production target. Financial persistence uses purpose-specific fixed-precision `NUMERIC` types and business calculations use `Decimal`; scientific indicators and research statistics use floating point.

See [AUTONOMOUS_TRADING_ROADMAP.md](AUTONOMOUS_TRADING_ROADMAP.md) for the capability map and [RESEARCH_AND_LEARNING.md](RESEARCH_AND_LEARNING.md) for validation gates and memory.
