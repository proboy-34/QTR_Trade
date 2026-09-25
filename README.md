# QTR — Quantitative Trading Research

QTR is a modular-monolith quantitative research and paper-trading platform. It observes an eligible multi-asset Binance universe, scans for unusual situations, records market/news context, researches and validates strategy hypotheses (with explicit overfitting safeguards), paper-trades validated candidates, remembers every decision and trade, and turns the accumulated evidence into new research — while real-money trading stays disabled. It preserves the evidence chain from a research objective through a validated, versioned strategy to decision, risk approval, execution, fill, and position. V1 is deliberately safe: it starts in **paper trading**, runs without exchange credentials, and refuses live mode unless two independent server-side gates are set.

## Autonomous research loop (new)

- **Multi-asset universe**: eligibility from live Binance exchange metadata (liquidity, spread, status, filters, data quality, abnormal moves) — no hard-coded coin list.
- **Market scanner, regimes and memory**: signal detection, ranked/deduplicated opportunities (never trades), evidence-scored regimes with `UNKNOWN` when unclear, similarity search over market history.
- **Market intelligence**: Finnhub news/economic calendar as structured, source-verified events; FRED macro series with point-in-time vintages.
- **Truthful integrations**: Binance, Gemini, Finnhub and FRED are verified with real requests; results (endpoint, latency, failure reason) are persisted and shown as HEALTHY / DEGRADED / UNAVAILABLE / STALE / NOT CONFIGURED / NOT VERIFIED. Nothing is shown as connected by assumption.
- **Evidence-based decisions**: every decision stores a structured rationale (supporting / contradicting / blocking evidence, timeframe roles, invalidation, plan, AI review). NO TRADE is a valid outcome.
- **AI researcher (Gemini)**: budgeted, rate-limited, audited; a hallucination firewall separates facts, interpretation and hypotheses; AI output becomes research artifacts, never orders.
- **Research engine**: declarative strategy specs (no code execution), IS/OOS/walk-forward, parameter perturbation, cost stress, Monte Carlo, deflated Sharpe with trial counting, passive benchmark, challengers vs champions.
- **Paper validation and human review**: candidates trade paper money; promotion is an operator decision.
- **Learning**: immutable trade memory, decision lineage, post-trade analysis, counterfactuals, strategy decay detection, backtest-vs-paper discrepancy, queryable knowledge base.
- **Portfolio intelligence and safety**: correlation/concentration/beta-aware sizing, and a persisted kill switch (system, paper trading, new orders, strategy, asset) with automatic safe mode.

See [docs/AUTONOMOUS_TRADING_ROADMAP.md](docs/AUTONOMOUS_TRADING_ROADMAP.md) (current → next → future) and [docs/RESEARCH_AND_LEARNING.md](docs/RESEARCH_AND_LEARNING.md) (gates, memory, AI rules).

## What is included

- Platform Core: validated configuration, JSON logging, UTC time, centralized errors, event bus, replaceable secret provider, Telegram notifications, lifecycle hooks, health/readiness endpoints, and an observable async task orchestrator.
- Global Trading Services: normalized Binance/OKX/Bybit public ticker adapters, a reconnecting Binance public WebSocket feed, market-data quality checks, asset registry, provider-neutral market intelligence, deterministic research analysis, next-bar backtesting, rolling walk-forward validation, and the versioned Strategy Repository.
- Research: scoped plans, persistent datasets/observations/hypotheses, a real experiment state machine with duplicate prevention and failure history, research review, and a durable knowledge-base view.
- Trading layers: persistent Market Memory and Opportunity Queue, reconstructable TRADE/WAIT/IGNORE decisions, directional Trade Intents, dedicated risk rejection and sizing, immutable Execution Plans, configurable/idempotent paper orders, partial fills, position lifecycle events, execution reports, and safe reconciliation.
- Premium responsive React/TypeScript terminal with live operational views, working forms, filters/tables, backtests, decisions, risk limits, health, events, and secret-safe integration status.
- SQLite zero-configuration demo plus PostgreSQL Docker deployment, Alembic migration, seed data, OpenAPI, unit/integration/E2E tests.

Implementation boundaries are documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and the requirement-by-requirement result is in [docs/ARCHITECTURE_AUDIT.md](docs/ARCHITECTURE_AUDIT.md). Operational guides cover [paper trading](docs/PAPER_TRADING.md), [market data](docs/MARKET_DATA.md), [recovery](docs/RECOVERY.md), [testing](docs/TESTING.md), [production readiness](docs/PRODUCTION_READINESS.md), and [safety](docs/SAFETY.md).

## Quick start (local demo)

Python 3.12+ and Node 22+ are required.

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m alembic upgrade head
python -m uvicorn app.main:app --reload
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. API docs are at `http://localhost:8000/docs`.

New screens: **Market overview**, **Opportunities**, **Intelligence**, **Research lab**, **Strategy health**, **Learning**, and **Safety & risk**. All figures come from the database; screens say "not configured" rather than inventing data when a provider or key is missing.

With internet access the scheduler discovers the Binance universe, syncs candles, scans, researches and learns automatically. To try the research engine offline, set `DEMO_MODE=true`, backfill the synthetic `paper` provider (Market data → Historical backfills) and create a hypothesis in the Research lab with data source "paper".

## Docker / PostgreSQL

```bash
docker compose up --build
```

Open `http://localhost:5173`. The API container waits for PostgreSQL and exposes port 8000.

## Verification

```powershell
python -m pytest -q
python -m ruff check app tests
python -m mypy app --ignore-missing-imports
python -m alembic upgrade head
python -m alembic check
cd frontend
npm test -- --run
npm run lint
npm run build
```

## Paper trading

The checked-in defaults are `TRADING_MODE=paper`, `EXECUTION_MODE=paper`, `DEMO_MODE=false` and `LIVE_TRADING_ENABLED=false`. Synthetic demo data exists only with `DEMO_MODE=true` and is labelled DEMO everywhere. From Decision Monitor, **Evaluate** runs the persisted pipeline on the latest stored closed candle (the server builds the snapshot; the browser never supplies prices). Backtesting uses the next candle for signal execution and includes fees and slippage.

The paper venue defaults to deterministic immediate fills for repeatable tests. `PAPER_IMMEDIATE_FILL` and `PAPER_PARTIAL_FILL_RATIO` allow queued and partial-fill scenarios without pretending they are live exchange behavior.

For real Binance market data with paper-only execution, open **Trading → Binance live paper**, keep `BTCUSDT` and `1h`, then select **Start Binance live paper**. QTR bootstraps recent public candles, connects to the public kline stream, evaluates on closed candles, monitors stops/targets from live updates, and records the run. No Binance key is required and the service cannot submit a Binance order.

## Live trading warning

This V1 does **not** ship a live private-order adapter. Setting live gates validates operator intent but cannot route orders until an authenticated adapter is implemented and reviewed. Never treat the demo/paper exchange as proof that a live adapter is safe. See [docs/SAFETY.md](docs/SAFETY.md).

## Configuration

Copy `.env.example` to `.env`. Secret values are consumed only by the backend and never returned by APIs. For production, set `DATABASE_URL=postgresql+psycopg://...` and install `.[postgres]`.

External configuration is listed in [MANUAL_SETUP.md](MANUAL_SETUP.md).

## Honest V1 boundaries

- No authenticated private exchange adapter is registered, so real orders cannot be sent.
- Real-provider connectivity (Binance public endpoints, Gemini, news providers) could not be exercised from the build environment; adapters are covered by fixture-based tests and must be smoke-tested where the network and keys are available.
- Historical backfills support bounded, checkpointed Binance/OKX/Bybit acquisition plus deterministic demo history; news/macro ingestion needs a configured provider key, otherwise events are operator-entered.
- Roles (viewer, researcher, trader, admin), a local demo identity and static bearer tokens (`AUTH_MODE=token`) are included, but external authentication is not. Put the application behind an authenticated private network boundary before public or multi-user deployment.
- SQLite is for local demo use. PostgreSQL is the production target for transactional concurrency and fixed-precision financial storage.
