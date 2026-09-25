# Manual setup required

Nothing external is required for the local SQLite paper/demo mode.

Binance live-data paper mode also requires no API key. It uses public historical and WebSocket market data only. Internet access to Binance public endpoints is required.

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
