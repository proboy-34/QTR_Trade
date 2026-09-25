# QTR — Quantitative Trading Research

QTR is a modular-monolith research and paper-trading platform. It preserves the evidence chain from a research objective through a validated, versioned strategy to decision, risk approval, execution, fill, and position. V1 is deliberately safe: it starts in **paper trading**, runs without exchange credentials, and refuses live mode unless two independent server-side gates are set.

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
cd frontend
npm test -- --run
npm run lint
npm run build
```

## Paper trading

The checked-in defaults are `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`. Demo data is visibly marked. From Decision Monitor, select **Evaluate market now** to run the persisted paper pipeline. Backtesting uses the next candle for signal execution and includes fees and slippage.

The paper venue defaults to deterministic immediate fills for repeatable tests. `PAPER_IMMEDIATE_FILL` and `PAPER_PARTIAL_FILL_RATIO` allow queued and partial-fill scenarios without pretending they are live exchange behavior.

For real Binance market data with paper-only execution, open **Trading → Binance live paper**, keep `BTCUSDT` and `1h`, then select **Start Binance live paper**. QTR bootstraps recent public candles, connects to the public kline stream, evaluates on closed candles, monitors stops/targets from live updates, and records the run. No Binance key is required and the service cannot submit a Binance order.

## Live trading warning

This V1 does **not** ship a live private-order adapter. Setting live gates validates operator intent but cannot route orders until an authenticated adapter is implemented and reviewed. Never treat the demo/paper exchange as proof that a live adapter is safe. See [docs/SAFETY.md](docs/SAFETY.md).

## Configuration

Copy `.env.example` to `.env`. Secret values are consumed only by the backend and never returned by APIs. For production, set `DATABASE_URL=postgresql+psycopg://...` and install `.[postgres]`.

External configuration is listed in [MANUAL_SETUP.md](MANUAL_SETUP.md).

## Honest V1 boundaries

- No authenticated private exchange adapter is registered, so real orders cannot be sent.
- Historical backfills support bounded, checkpointed Binance/OKX/Bybit acquisition plus deterministic demo history; macro/news events remain manual/provider-neutral.
- A provider-neutral role model and local demo identity are included, but external authentication is not. Put the application behind an authenticated private network boundary before public or multi-user deployment.
- SQLite is for local demo use. PostgreSQL is the production target for transactional concurrency and fixed-precision financial storage.
