# Paper trading

The paper venue consumes an approved `ExecutionPlan` without changing its symbol, side, size, leverage, stop, or target. It validates exchange instrument metadata, then records an idempotent client order.

Supported deterministic states are `SUBMITTED`, `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, and `REJECTED`. Market, limit, and stop triggers are supported. Fees, directional slippage, partial-fill ratio, minimum quantity/notional, tick size, and step size are enforced. A repeated submission for the same plan returns the existing order.

The paper loop validates each snapshot, invokes Decision, passes a resulting intent to Risk, executes only an approved plan, marks open positions, and closes positions at their stored stop or target. `WAIT`, `IGNORE`, and rejected risk assessments create no order.

## Binance live-data paper mode

Use **Trading → Binance live paper** to connect real public Binance market data to the paper loop. Starting a session:

1. Loads a bounded set of recent closed candles for indicator warm-up.
2. Connects to the Binance public kline WebSocket with ping/pong and reconnect backoff.
3. Validates and deduplicates every closed candle before persistence.
4. Runs Decision only when the active strategy symbol and timeframe match the feed.
5. Updates paper-position marks from live prices and enforces the stored stop/target.
6. Persists session status, candle/decision counts, reconnects, timestamps, and errors.

Live data does not mean live execution. The status API always reports `paper_only=true` and `real_orders_enabled=false`. No private Binance adapter or authenticated order route is registered.

Useful settings:

- `PAPER_IMMEDIATE_FILL`: leave eligible orders pending when false.
- `PAPER_PARTIAL_FILL_RATIO`: deterministic first-fill fraction.
- `PAPER_FEE_RATE`: fee charged on notional.
- `PAPER_SLIPPAGE_RATE`: deterministic adverse fill adjustment.

This simulator is for research and operational rehearsal. It does not reproduce queue position, venue-specific liquidation, or every matching-engine rule.
