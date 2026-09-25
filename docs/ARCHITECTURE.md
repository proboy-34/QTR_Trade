# QTR implementation architecture

QTR is one FastAPI application and one React client backed by one relational database. Conceptual services remain separate Python modules and do not imply network services.

```text
Platform Core
  ├─ configuration · lifecycle · time · logging · health · scheduler · secrets
  └─ in-process typed Event Bus
         │
Global Services
  ├─ historical adapters + reconnecting Binance live stream + normalization
  ├─ features + market intelligence
  ├─ validation/backtesting
  ├─ assets + time
  └─ Strategy Repository  ← only Research/Trading bridge
         │
   ┌─────┴───────────────┐
Research                Trading
plans → experiments     observation → context → eligibility → decision
→ evidence → strategy                         ↓
→ validation → repo                     trade intent
                                              ↓
                                      portfolio & risk
                                              ↓
                                       execution plan
                                              ↓
                                      paper exchange
                                              ↓
                                    order → fill → position
```

Research code writes candidate strategies and immutable versions to the repository. Decision reads only active repository versions, persists market context and opportunities, and emits a directional Trade Intent. It never creates strategies or calculates final size, leverage, stop, or target. Portfolio/Risk can reject the intent and is the only layer that creates those final values in an immutable Execution Plan. Execution consumes that plan unchanged.

The live-paper coordinator bootstraps public Binance history, consumes validated closed WebSocket candles, runs Decision only for a matching strategy timeframe, and monitors stored stops/targets from intra-candle live prices. The paper exchange models submitted, new, partial, filled, cancelled, and rejected orders; supports deterministic market, limit, and stop triggers; applies configured slippage/fees; validates Asset Registry constraints; and enforces order/fill idempotency. Position management records mark, break-even, partial close, managing, and closed transitions. Recovery and reconciliation compare reported exchange state with persisted orders/positions and create issues; they never submit corrective orders.

The relational schema covers research plans, reproducible experiments, datasets, market candles and checkpointed backfills, validation failures, market contexts, observations, hypotheses, strategies and versions, validation and status history, intelligence events, opportunities, decisions, trade intents, risk events, execution plans/reports, orders, fills, positions/events, reconciliation issues, snapshots, exchange instruments, connection metadata, job executions, and versioned system events.

SQLite is the developer/demo default. PostgreSQL is the container and production target. Financial persistence uses purpose-specific fixed-precision `NUMERIC` types and business calculations use `Decimal`; scientific indicators use floating point. JSON columns contain explicit rule structures and evidence while lifecycle relations remain indexed relational keys.

See [ARCHITECTURE_AUDIT.md](ARCHITECTURE_AUDIT.md) for verified coverage and remaining deviations.
