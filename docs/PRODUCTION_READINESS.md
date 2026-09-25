# Production readiness

QTR is ready for a single-operator local demo and deterministic paper trading. It is not ready for real-money trading or direct public internet exposure.

## Readiness by environment

| Environment | Status | Requirements |
| --- | --- | --- |
| Local demo | Ready | SQLite, synthetic market history, local demo identity |
| Paper trading | Ready for supervised use | PostgreSQL recommended, monitored market-data connection, backups |
| Sandbox/testnet | Interface-ready only | A reviewed authenticated adapter still must be implemented |
| Future live | Not supported | Security review, external identity, HA, live adapter, operational runbooks |

Financial values use `Decimal` in trading/accounting code and fixed-precision `NUMERIC` columns. Statistical features continue to use floating point. Migration `0003_precision_backfill_hardening` casts existing values and backfills new required identifiers before constraints are applied.

Before any hosted paper deployment, use PostgreSQL, TLS, an authenticated private network boundary, database backups, explicit CORS origins, and external secret storage. Review all open `PARTIAL` and `MISSING` entries in `ARCHITECTURE_AUDIT.md`.

No private exchange order adapter is installed. The live configuration gate does not provide live execution.
