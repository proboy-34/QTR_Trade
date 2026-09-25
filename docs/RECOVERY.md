# Recovery and reconciliation

Recovery reloads pending/new/partially-filled orders and open/managing positions from persistent storage. It compares them with a provider-neutral exchange-state snapshot and records discrepancies as reconciliation issues. It records a `RecoveryCompleted` system event with affected IDs and whether manual intervention is required.

Recovery deliberately creates zero corrective orders. It never changes an approved execution plan, replays an order, or invents a missing fill. Execution uses stable client order IDs and fills use unique execution keys to make duplicates safe.

If the process exits while a live-paper session is active, startup marks that session `INTERRUPTED`. Persisted orders and positions remain available for reconciliation. Set `LIVE_PAPER_AUTOSTART=true` only when an operator intentionally wants the configured public-data paper session restarted with the application; it still cannot route real orders.

Operator procedure after interruption:

1. Start the application and confirm database readiness.
2. Call `POST /api/v1/recovery` with the observed paper/sandbox order and position state.
3. Inspect `/api/v1/reconciliation/issues`, pending orders, positions, fills, and system events.
4. Resolve discrepancies manually; do not delete audit history.

For local paper mode, persisted SQLite state is recoverable but PostgreSQL is recommended for concurrent or continuously running use.
