"""Financial precision, historical backfills and operational hardening.

Existing numeric values are converted by database CAST during batch-table copy.
SQLite uses NUMERIC affinity for demo compatibility; PostgreSQL enforces precision.
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0003_precision_backfill_hardening"
down_revision = "0002_architecture_finalization"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)
QUANTITY = sa.Numeric(30, 12)
MONEY = sa.Numeric(30, 10)
RATE = sa.Numeric(20, 12)

NUMERIC_COLUMNS = {
    "market_data": {"open": PRICE, "high": PRICE, "low": PRICE, "close": PRICE, "volume": QUANTITY, "funding_rate": RATE, "open_interest": MONEY},
    "trade_intents": {"entry_price": PRICE, "stop_loss": PRICE, "take_profit": PRICE, "confidence": RATE},
    "execution_plans": {"quantity": QUANTITY, "risk_amount": MONEY, "stop_loss": PRICE, "take_profit": PRICE, "leverage": RATE, "limit_price": PRICE},
    "positions": {"quantity": QUANTITY, "entry_price": PRICE, "current_price": PRICE, "stop_loss": PRICE, "take_profit": PRICE, "unrealized_pnl": MONEY, "realized_pnl": MONEY},
    "orders": {"quantity": QUANTITY, "fill_quantity": QUANTITY, "average_fill_price": PRICE, "fees": MONEY},
    "fills": {"quantity": QUANTITY, "price": PRICE, "fee": MONEY},
    "portfolio_snapshots": {"equity": MONEY, "available_balance": MONEY, "exposure": RATE, "margin_used": MONEY, "daily_pnl": MONEY, "drawdown": RATE},
    "assets": {"tick_size": PRICE, "step_size": QUANTITY, "min_quantity": QUANTITY, "min_notional": MONEY},
    "market_contexts": {"price": PRICE, "funding": RATE},
    "position_events": {"price": PRICE, "quantity": QUANTITY},
}


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _column_names(table):
        op.add_column(table, column)


def _has_unique(table: str, columns: tuple[str, ...]) -> bool:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints(table)
    return any(tuple(item.get("column_names") or ()) == columns for item in constraints)


def _convert(table: str, columns: dict[str, sa.Numeric], target_numeric: bool) -> None:
    existing = {column["name"]: column["type"] for column in sa.inspect(op.get_bind()).get_columns(table)}
    changes = []
    for name, numeric_type in columns.items():
        current = existing.get(name)
        if current is None:
            continue
        is_fixed_numeric = isinstance(current, sa.Numeric) and not isinstance(current, sa.Float)
        if is_fixed_numeric != target_numeric:
            changes.append((name, current, numeric_type if target_numeric else sa.Float()))
    if changes:
        with op.batch_alter_table(table) as batch:
            for name, current, target in changes:
                batch.alter_column(name, existing_type=current, type_=target)


def upgrade() -> None:
    _add("experiments", sa.Column("dataset_id", sa.String(36), nullable=True))
    _add("experiments", sa.Column("strategy_version_id", sa.String(36), nullable=True))
    _add("experiments", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    _add("experiments", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    _add("experiments", sa.Column("runtime_ms", sa.Integer(), nullable=True))
    _add("experiments", sa.Column("reproducibility", sa.JSON(), nullable=True))
    _add("execution_plans", sa.Column("stop_price", PRICE, nullable=True))
    _add("positions", sa.Column("fees", MONEY, nullable=True, server_default="0"))
    _add("positions", sa.Column("exit_reason", sa.String(100), nullable=True))
    _add("fills", sa.Column("execution_key", sa.String(100), nullable=True))
    _add("market_events", sa.Column("event_type", sa.String(50), nullable=True))
    _add("market_events", sa.Column("title", sa.String(300), nullable=True))
    _add("market_events", sa.Column("source_url", sa.String(1000), nullable=True))
    _add("system_events", sa.Column("event_id", sa.String(36), nullable=True))
    _add("system_events", sa.Column("source", sa.String(80), nullable=True))
    _add("system_events", sa.Column("version", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE fills SET execution_key = 'legacy-' || id WHERE execution_key IS NULL"))
    op.execute(sa.text("UPDATE market_events SET event_type = category, title = description WHERE event_type IS NULL"))
    op.execute(sa.text("UPDATE system_events SET event_id = id, source = component, version = 1 WHERE event_id IS NULL"))
    op.execute(sa.text("UPDATE execution_plans SET stop_loss = COALESCE(stop_loss, (SELECT stop_loss FROM trade_intents WHERE trade_intents.id = execution_plans.trade_intent_id)), take_profit = COALESCE(take_profit, (SELECT take_profit FROM trade_intents WHERE trade_intents.id = execution_plans.trade_intent_id)), leverage = COALESCE(leverage, 1)"))
    for table, columns in NUMERIC_COLUMNS.items():
        _convert(table, columns, True)
    fills_unique = _has_unique("fills", ("execution_key",))
    with op.batch_alter_table("fills") as batch:
        batch.alter_column("execution_key", existing_type=sa.String(100), nullable=False)
        if not fills_unique:
            batch.create_unique_constraint("uq_fills_execution_key", ["execution_key"])
    with op.batch_alter_table("trade_intents") as batch:
        batch.alter_column("stop_loss", existing_type=PRICE, nullable=True)
        batch.alter_column("take_profit", existing_type=PRICE, nullable=True)
    with op.batch_alter_table("execution_plans") as batch:
        batch.alter_column("stop_loss", existing_type=PRICE, nullable=False)
        batch.alter_column("take_profit", existing_type=PRICE, nullable=False)
        batch.alter_column("leverage", existing_type=RATE, nullable=False)
    events_unique = _has_unique("system_events", ("event_id",))
    with op.batch_alter_table("system_events") as batch:
        batch.alter_column("event_id", existing_type=sa.String(36), nullable=False)
        batch.alter_column("source", existing_type=sa.String(80), nullable=False)
        batch.alter_column("version", existing_type=sa.Integer(), nullable=False)
        if not events_unique:
            batch.create_unique_constraint("uq_system_events_event_id", ["event_id"])
    for name in (
        "asset_instruments",
        "market_data_backfills",
        "market_data_validation_failures",
        "job_executions",
    ):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in (
        "job_executions",
        "market_data_validation_failures",
        "market_data_backfills",
        "asset_instruments",
    ):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
    for table, columns in reversed(tuple(NUMERIC_COLUMNS.items())):
        _convert(table, columns, False)
