# Trading safety

- Paper trading is the default and the only executable adapter included in V1.
- A configuration declaring `TRADING_MODE=live` is rejected unless both `LIVE_TRADING_ENABLED=true` and `LIVE_TRADING_CONFIRMATION=ENABLE_REAL_ORDERS` are present.
- Those gates express intent only. No private live order route exists in V1.
- Active strategy versions cannot be edited. New work becomes a new version after suspension.
- Approval and activation require persisted `PASS` validation evidence.
- Trade Intent contains no final stop, target, size, or leverage. Risk owns all final plan values.
- Risk rejects zero capital, insufficient margin, excessive daily loss/drawdown/exposure, maximum positions, invalid market inputs, and exchange minimum violations before an Execution Plan exists.
- Paper orders use stable client order IDs derived from execution plan IDs.
- Paper fills are deterministic by default but can be queued or partially filled through configuration.
- Exchange reconciliation reports discrepancies; it must not make corrective trades automatically.
- Recovery reports pending/open state and persists manual-intervention requirements; it creates no corrective orders.
- Financial trade/account values use fixed-precision Decimal/NUMERIC handling; indicator statistics remain floating point.
- Secrets remain server-side and logs/status endpoints expose configuration state, never secret values.
- Live environments reject wildcard CORS. No debug or live-order endpoint is enabled.
