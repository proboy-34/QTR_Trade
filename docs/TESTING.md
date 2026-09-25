# Testing

Backend tests are deterministic and use an in-memory database, synthetic candles, and fake connection probes. They cover data integrity/backfills, research and strategy lifecycle, no-look-ahead backtesting, Decision boundaries, risk rejections, paper order/fill lifecycle, Decimal accounting, positions, event idempotency, reconciliation/recovery, scheduling, API behavior, and the full research-to-close evidence chain.

Run all checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m mypy app --ignore-missing-imports
Set-Location frontend
npm test -- --run
npm run lint
npm run build
```

Migration validation uses temporary databases: upgrade a blank database to `head`, start and seed the app, downgrade, re-upgrade, then repeat the upgrade against a copy of a populated pre-`0003` database and compare row counts/financial values.

External exchanges are not required for the test suite. Real credentials and real orders must never be used in automated tests.
