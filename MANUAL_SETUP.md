# Manual setup required

Nothing external is required for the local SQLite paper/demo mode.

Binance live-data paper mode also requires no API key. It uses public historical and WebSocket market data only. Internet access to Binance public endpoints is required.

Optional autonomous-research configuration (everything else runs without it):

- [ ] `GEMINI_API_KEY` — enables the AI research assistant (market context, hypothesis proposals, trade and degradation interpretation). Adjust `AI_DAILY_BUDGET`, `AI_MONTHLY_BUDGET`, `AI_MAX_REQUESTS_PER_HOUR` and the per-token cost settings for your model.
- [ ] `FINNHUB_API_KEY` — crypto news and (if your plan includes it) the economic calendar. Verification reports `NOT_AVAILABLE_ON_PLAN` rather than pretending. Without it, only operator-entered events exist; QTR never invents news.
- [ ] `FRED_API_KEY` — macro series stored with observation date, vintage (release) date, availability time and retrieval time, so research and decisions only see values that were public at the time.
- [ ] Network access to `api.binance.com`, `data-api.binance.vision`, `stream.binance.com`, `generativelanguage.googleapis.com`, `finnhub.io` and `api.stlouisfed.org` (plus `testnet.binance.vision` for testnet mode).
- [ ] After configuring, run **System health → Verify providers now** (or `POST /api/v1/system/providers/verify`). Until a provider passes a real check it is shown as `NOT VERIFIED`, never as connected.

Optional production and integration configuration:

- [ ] PostgreSQL `DATABASE_URL` (Docker Compose supplies a local database automatically)
- [ ] Binance mainnet API key **and secret** — read-only, withdrawals and transfers disabled, IP-restricted. Used only to verify permissions; without the secret the key is reported as `SKIPPED` (unverified).
- [ ] Binance Spot Testnet key and secret (`BINANCE_TESTNET_API_KEY/SECRET`) and `EXECUTION_MODE=testnet` — only if you want testnet execution instead of internal paper fills.

Do not provide withdrawal-enabled exchange keys. OKX, Bybit and Telegram settings are not used by this build.

Before an internet-exposed or multi-user deployment:

- [ ] Replace the local demo identity with an external authentication provider; operator, researcher, and auditor roles already have a provider-neutral contract
- [ ] Set an explicit production `CORS_ORIGINS` allowlist
- [ ] Use PostgreSQL rather than the SQLite demo database
- [ ] Put TLS and an authenticated network boundary in front of the application

These are deployment requirements, not requirements for local paper/demo operation.
