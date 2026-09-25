# Testing

Backend tests are deterministic (currently 122 backend and 11 frontend tests) and use an in-memory database, synthetic candles, and fake connection probes. They cover data integrity/backfills, research and strategy lifecycle, no-look-ahead backtesting, Decision boundaries, risk rejections, paper order/fill lifecycle, Decimal accounting, positions, event idempotency, reconciliation/recovery, scheduling, API behavior, and the full research-to-close evidence chain.

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

## Autonomous research coverage

- `test_universe_scanner_regime.py`: exchange-metadata parsing, every eligibility exclusion, multi-asset refresh and paper-constraint mirroring, regime classification/uncertainty/no-look-ahead/transitions, scanner detectors, opportunity dedup and news linkage.
- `test_intelligence_ai.py`: provider parsing, structured normalization, dedup, macro updates, AI gateway (unconfigured, retry, non-retryable, timeout, budget, rate limit), hallucination firewall, malformed responses, AI hypotheses and malicious specs, Gemini request shape and secret redaction (mocked transport).
- `test_research_pipeline.py`: DSL safety, no-look-ahead, conservative stop/target, cost sensitivity, deflated Sharpe/Monte Carlo/perturbation/passive benchmark, full lifecycle to paper testing, reproducible experiments, random-walk rejection, insufficient data, challenger vs champion.
- `test_memory_learning.py`: immutable trade memory and MAE/MFE, post-trade analysis, counterfactuals, degradation with follow-up research, paper validation and discrepancy, market similarity, decision reconstruction, knowledge base.
- `test_portfolio_safety.py`: correlation/concentration/beta, risk sizing and cluster rejection, kill-switch scopes, safe-mode triggers, impossible prices, and a source scan proving no live-order path exists.
- `test_live_multi_asset_jobs.py`: combined-stream routing, reconnect/backoff, persisted job runner, SYSTEM stop.
- `test_regressions_and_api.py`: fixed defects, token auth and roles, the autonomy API surface.
- `test_full_loop_integration.py`: market data → universe → scanner → news → AI (fixture provider) → hypothesis → IS/OOS/walk-forward/robustness → paper decisions → risk → orders → positions → exits → trade memory → post-trade analysis → paper validation → human promotion → new research → restart.

Only external services are replaced by deterministic fixtures (Binance metadata, news, Gemini); fixtures live in `tests/market_fixtures.py` and are never imported by application code.

Migration `0005` is verified on a fresh database (upgrade → downgrade → re-upgrade, `alembic check` clean) and on a populated `0004` database created by the previous release (row counts and financial values compared after upgrade, downgrade and re-upgrade).
