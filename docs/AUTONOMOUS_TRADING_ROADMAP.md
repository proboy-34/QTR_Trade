# QTR autonomous research roadmap

Audit and implementation date: 2026-09-25. This document maps the autonomous
quantitative-research brief onto the repository as it actually is: what existed before
this work, what was built now, what is only partial, and what remains.

`QTR_Full_Architecture-2.md` is not present in the repository. The architecture
described in `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE_AUDIT.md` and the supplied brief
was treated as the source of truth; no replacement architecture was invented. QTR stays a
modular monolith: one FastAPI process, one React client, one relational database.

## Invariants that did not change

- `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false` are the defaults. Live mode still
  requires two gates, and even then no authenticated order adapter exists.
- Research → Strategy Repository → Trading. Research code writes candidates only; it cannot
  approve or activate a strategy (`StrategyRepository.transition(..., actor="research")`
  raises for `approved`/`active`). Promotion is a human operator action.
- Decision → Trade Intent → Risk → Execution Plan → Paper Execution. Risk can reject; a
  rejected intent never reaches execution. The paper venue re-checks the kill switch.
- AI is a researcher, never an order authority, and never a source of facts.

## Status legend

| Status | Meaning |
| --- | --- |
| IMPLEMENTED | Built, wired, and covered by automated tests |
| PARTIAL | Useful and tested, but a named part of the brief is not done |
| NEEDS CREDENTIALS | Code path exists; untested against the real provider without a key |
| NEEDS HARDENING | Works, but needs more operational evidence before long unattended runs |
| NEEDS RESEARCH | Deliberately not built: requires more data or statistical justification |
| MISSING | Not built |

## Baseline found before this work

Verified by running the repository, not by reading the previous report:

- 63 backend tests passed; Ruff clean; mypy reported 1 error (`app/api.py`); 5 frontend
  tests, ESLint and the production build passed. Alembic head was `0004_live_binance_paper`.
- Present: platform core, event bus with persisted audit events, scheduler (placeholder
  jobs), research plans/experiments/observations/hypotheses tables, EMA-crossover backtest,
  walk-forward validator, validation-gated Strategy Repository, Decision → Intent → Risk →
  Plan → Paper exchange pipeline, positions, reconciliation/recovery, public Binance/OKX/
  Bybit tickers and historical backfills, single-symbol Binance live-paper stream, manual
  market events, Telegram notifications, a black/gold React terminal.
- Defects found during this work and fixed (with regression tests):
  - Paper MARKET orders were rejected with `INVALID_PRICE_TICK` whenever the observed price
    was not on the instrument tick grid (i.e. almost always) — the demo "evaluate" flow
    produced a rejected order and no position.
  - Live paper never reported its last decision (`last_decision` read the wrong key).
  - Resuming a backfill from SQLite crashed (naive vs aware datetimes); on a non-UTC host a
    naive `.timestamp()` would also shift Binance request windows by the local offset.
  - The dashboard "BTC / USDT" hero showed the latest candle of any symbol, and the chart
    could mix demo and exchange candles.
  - Generic registry tables in the UI rendered blank cells for ordinary columns.
  - The top-bar "SYSTEM HEALTHY" badge and System Health cards were hard-coded.

## Capability map: CURRENT → NEXT → FUTURE

| # | Capability | Before | Now | Where | Next / remaining |
| --- | --- | --- | --- | --- | --- |
| 9–10 | Dynamic multi-asset universe and eligibility | MISSING (BTCUSDT seeded) | IMPLEMENTED | `app/global_services/universe.py` | Real Binance run not possible from the build sandbox (proxy 403); verify on the operator's machine |
| 10 | Eligibility: status, quote, liquidity, spread, filters, data quality, history, abnormal moves, stablecoins/leveraged tokens | MISSING | IMPLEMENTED | `EligibilityEngine` | Add Binance "monitoring"/seed-tag lists when the public endpoint exposes them |
| 11 | Market scanner (volume, volatility, breakout, momentum, trend, abnormal move, liquidity, market-wide, correlation) | MISSING | IMPLEMENTED | `app/global_services/scanner.py` | Tune thresholds from recorded counterfactual evidence |
| 35 | Opportunity ranking (liquidity, data quality, regime clarity, signal strength, strategy applicability, news, risk) | Score inside pipeline only | IMPLEMENTED | `OpportunityRanker` | Weights are reviewed constants; revisit once enough outcomes exist |
| 12, 33 | Regime engine with confidence, evidence, UNKNOWN on ambiguity, persisted transitions | Two labels (trending/sideways) | IMPLEMENTED | `app/global_services/regime.py` | Cross-asset (market-wide) regime is not yet a separate label |
| 13–15 | Provider-neutral news/macro intelligence, structured events, dedup, source identity, macro surprise | Manual events only | IMPLEMENTED / NOT VERIFIED LIVE | `app/global_services/market_intelligence.py`, `app/integrations/fred.py` | Finnhub news/calendar and FRED point-in-time series. Keys configured locally but the build sandbox's network policy blocks finnhub.io and api.stlouisfed.org (see docs/AUDIT_2026-09-25.md). CryptoPanic removed. |
| 15, 40 | Hallucination firewall (FACT / INTERPRETATION / HYPOTHESIS / METRIC; UNVERIFIED) | MISSING | IMPLEMENTED | `app/ai/firewall.py` | — |
| 16, 18 | AI provider abstraction + Gemini adapter + research tasks | MISSING | IMPLEMENTED / NEEDS CREDENTIALS | `app/ai/providers.py`, `app/ai/research.py` | Gemini tested with a mocked HTTP transport only |
| 41 | AI cost control: budgets, hourly limit, token/cost/latency/failure records, caching | MISSING | IMPLEMENTED | `app/ai/gateway.py` | Cost rates are configurable approximations |
| 17, 20 | AI cannot bypass validation; no code generation/execution | n/a | IMPLEMENTED | `app/research/dsl.py` | — |
| 19 | Hypothesis lifecycle IDEA → … → ACTIVE, with REJECTED/FAILED/DEGRADED/ARCHIVED | Free-text hypotheses | IMPLEMENTED | `app/research/hypotheses.py` | Multi-timeframe specs are validated on their primary timeframe only |
| 20–21 | Declarative strategy DSL, compatible with existing repository rules | Rules stored, not executed | IMPLEMENTED | `app/research/dsl.py`, `spec_backtest.py` | Long-only; shorts and cross-timeframe features are future work |
| 22, 24 | Overfitting protection and data-snooping awareness (trial counts, deflated Sharpe, IS→OOS decay, passive benchmark, parameter count) | MISSING | IMPLEMENTED | `app/research/robustness.py` | Gate calibration: 0/30 random walks and 10/10 genuine synthetic edges pass today; recalibrate on real data |
| 23 | Robustness: perturbation, cost stress, Monte Carlo, regime breakdown | MISSING | IMPLEMENTED | `RobustnessEvaluator` | Block bootstrap for autocorrelated returns |
| 31 | Challenger vs unchanged champion | MISSING | IMPLEMENTED | `app/research/challengers.py` | — |
| 9, 53 | Continuous paper validation → READY FOR REVIEW → human promotion | MISSING | IMPLEMENTED | `app/learning/paper_validation.py`, Strategy statuses `paper_testing`, `ready_for_review` | — |
| 38 | Backtest vs paper discrepancy engine | MISSING | IMPLEMENTED | `DiscrepancyReport` | Latency attribution needs live-venue timestamps |
| 25 | Market memory + historical similarity search | Contexts stored | IMPLEMENTED | `app/memory/market_memory.py` | Similarity is standardized Euclidean; no learned embedding (by design) |
| 26, 39 | Decision memory and data lineage (code version, strategy hashes, risk config, events, regime, AI artifacts, candles) | Evaluations stored | IMPLEMENTED | `TradingPipeline._lineage`, `reconstruct_decision` | Dataset snapshots are fingerprinted, not copied |
| 27 | Immutable trade memory (MAE/MFE, slippage, regime, events, R multiple) | Positions only | IMPLEMENTED | `app/memory/trade_memory.py` | — |
| 28–29 | "Why did we not trade?" + counterfactual outcomes | MISSING | IMPLEMENTED | `app/learning/counterfactual.py` | Informational only, as required |
| 30 | Post-trade analyst (deterministic facts; optional AI interpretation) | MISSING | IMPLEMENTED | `app/learning/post_trade.py` | — |
| 32 | Strategy decay detection with follow-up research, no silent changes | MISSING | IMPLEMENTED | `app/learning/strategy_health.py` | — |
| 65 | Queryable knowledge base | Counts endpoint | IMPLEMENTED | `app/memory/knowledge.py` | Full-text search is SQL `LIKE`; PostgreSQL FTS later |
| 34 | Portfolio intelligence: correlation, concentration (HHI), beta, correlated-cluster sizing in Risk | Exposure cap only | IMPLEMENTED | `app/trading/portfolio_intelligence.py`, `RiskEngine` | Stress correlation (regime-conditional) is future work |
| 37 | Realistic paper execution: spread, slippage, tick grid, gaps, partial fills, exits at adverse prices, strategy exit rules | Partial | IMPLEMENTED | `paper_exchange.py`, `loop.py` | Order-book depth/queue position are not simulated |
| 36 | Multi-timeframe: strategies declare timeframes; decisions only on matching timeframe | Timeframe filter | IMPLEMENTED | `TradingPipeline` | Cross-timeframe signals (e.g. 4h filter for 1h entries) are NEEDS RESEARCH |
| 44 | Kill switch: SYSTEM / PAPER_TRADING / NEW_ORDERS / STRATEGY / ASSET; automatic safe mode | MISSING | IMPLEMENTED | `app/trading/safety.py` | Database-failure trigger relies on the scheduler surviving; add an external watchdog |
| 45, 62 | 24/7 scheduling: persisted, idempotent, restart-aware jobs | Placeholder jobs | IMPLEMENTED / NEEDS HARDENING | `app/jobs.py` | Needs multi-day soak run against real Binance; single-process scheduler (no distributed lock) |
| 9, 68 | Multi-asset Binance live paper (combined stream), extending existing service | Single symbol | IMPLEMENTED / NEEDS CREDENTIALS-FREE REAL TEST | `live_paper.py` | Real WebSocket run not possible from the sandbox |
| 61 | Authentication foundation: viewer/researcher/trader/admin, static bearer tokens | Local demo principal | PARTIAL | `app/core/auth.py` | External identity provider (OIDC) before any public deployment |
| 47–49 | Premium UI: market overview, opportunities, intelligence, research lab, strategy health, learning, safety & system | Partial | IMPLEMENTED | `frontend/src/pages/*` | Charts for equity/regime history per asset |
| 50 | "No fake ML" | n/a | Respected | — | ML only after enough labelled evidence (NEEDS RESEARCH) |
| 54 | Real-money trading | Disabled | Disabled | `docs/SAFETY.md` | Out of scope |

## The loop today

```text
Binance public data ──► market-data sync / live stream ──► validation & persistence
      │                                                        │
      ▼                                                        ▼
Universe & eligibility ──► Scanner ──► Opportunities ◄── Regime engine ◄── News/macro (provider-neutral)
                                          │
                     ┌────────────────────┴───────────────────┐
                     ▼                                        ▼
         Deterministic research generator              AI researcher (budgeted, firewalled)
                     └──────────────► Hypotheses ◄────────────┘
                                          │  IS backtest → OOS → walk-forward → robustness
                                          ▼
                              Strategy Repository (paper_testing)
                                          │
Decision (declarative entry/exit on closed candles) ──► Risk (portfolio, kill switch) ──► Paper venue
                                          │
                       Positions ──► Trade memory ──► Post-trade analysis ──► Knowledge base
                                          │                      │
                         Paper validation / discrepancy     Strategy health / counterfactuals
                                          │                      │
                            READY FOR REVIEW (human)      New research hypotheses
```

## NEXT (in dependency order)

1. Run the full loop against real Binance public data on the operator's machine for
   several days (the build sandbox blocks Binance). Watch `/api/v1/system/jobs`,
   `/api/v1/safety` and the data-integrity job; tune scan/sync intervals to observed load.
2. Allow finnhub.io and api.stlouisfed.org, run **Verify providers now**, and confirm
   Finnhub plan coverage (news vs economic calendar) and FRED vintage ingestion on real payloads.
3. Observe Gemini costs against `AI_DAILY_BUDGET` (generation verified live on 2026-09-25) and
   review the first AI hypotheses, trade reviews and their firewall verdicts.
4. Recalibrate validation gates on real multi-asset history (false-positive rate on
   shuffled/benchmark strategies) and record the calibration as research evidence.
5. PostgreSQL for continuous operation (SQLite remains the local default).
6. Block/stationary bootstrap for Monte Carlo; regime-conditional correlation stress.

## FUTURE

- Short-side and cross-timeframe specifications in the DSL.
- Order-book-aware paper fills (depth, queue position) using public depth streams.
- External identity (OIDC) and per-user research workspaces.
- Statistical learning models only once labelled evidence (trade memory, counterfactuals)
  is large enough to validate them out-of-sample — the same gates apply.
- A reviewed, authenticated private execution adapter is explicitly *not* planned here;
  real-money trading remains disabled.

## External credentials

| Provider | Setting | Required for | Status in this work |
| --- | --- | --- | --- |
| Binance public data | none | Universe, candles, live stream | NOT VERIFIED — build sandbox network policy returns 403 for Binance hosts |
| Binance mainnet key | `BINANCE_API_KEY/SECRET` | Read-only permission check only | Key present, secret absent → SKIPPED (unverified) |
| Binance Spot Testnet | `BINANCE_TESTNET_API_KEY/SECRET`, `EXECUTION_MODE=testnet` | Testnet execution | Adapter tested with mocked HTTP; no testnet keys provided |
| Gemini | `GEMINI_API_KEY`, `GEMINI_MODEL` | AI research, trade review | VERIFIED 2026-09-25 (gemini-3.8-flash; real structured generation) |
| Finnhub | `FINNHUB_API_KEY` | News + economic calendar | NOT VERIFIED — host blocked by sandbox network policy |
| FRED | `FRED_API_KEY` | Point-in-time macro series | NOT VERIFIED — host blocked by sandbox network policy |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Notifications | Unchanged |
