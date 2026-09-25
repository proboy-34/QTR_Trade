# Manual setup required

Nothing external is required for the local SQLite paper/demo mode.

Binance live-data paper mode also requires no API key. It uses public historical and WebSocket market data only. Internet access to Binance public endpoints is required.

Optional autonomous-research configuration (everything else runs without it):

- [ ] `GEMINI_API_KEY` — enables the AI research assistant (market context, hypothesis proposals, trade and degradation interpretation). Adjust `AI_DAILY_BUDGET`, `AI_MONTHLY_BUDGET`, `AI_MAX_REQUESTS_PER_HOUR` and the per-token cost settings for your model.
- [ ] `NEWS_PROVIDER` (`cryptopanic` or `finnhub`) and `NEWS_PROVIDER_API_KEY` — scheduled news ingestion. Without it, only operator-entered events exist; QTR never invents news.
- [ ] `MACRO_PROVIDER=finnhub` and `MACRO_PROVIDER_API_KEY` — economic calendar with expected/actual/surprise.
- [ ] Internet access to `api.binance.com` and `stream.binance.com` — universe discovery, market-data sync, scanning and live paper trading (public, no key).

Optional production and integration configuration:

- [ ] PostgreSQL `DATABASE_URL` (Docker Compose supplies a local database automatically)
- [ ] Binance API key and secret
- [ ] OKX API key, secret, and passphrase
- [ ] Bybit API key and secret
- [ ] Telegram bot token and chat ID

Exchange credentials are currently retained for connectivity metadata and future authenticated adapters. Public ticker connections need no credentials. Do not provide withdrawal-enabled exchange keys; use least-privilege, IP-restricted keys when private adapters are added.

Before an internet-exposed or multi-user deployment:

- [ ] Replace the local demo identity with an external authentication provider; operator, researcher, and auditor roles already have a provider-neutral contract
- [ ] Set an explicit production `CORS_ORIGINS` allowlist
- [ ] Use PostgreSQL rather than the SQLite demo database
- [ ] Put TLS and an authenticated network boundary in front of the application

These are deployment requirements, not requirements for local paper/demo operation.
