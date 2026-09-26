"""REST closed-candle paper loop: processed-candle ledger, paper order provenance,
position monitoring state and explicit strategy validation summaries.

Revision ID: 0007_rest_paper_loop
Revises: 0006_truthful_integrations
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0007_rest_paper_loop"
down_revision = "0006_truthful_integrations"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(30, 10)
PRICE = sa.Numeric(30, 12)
NEW_TABLES = ("processed_candles",)
NEW_COLUMNS = {
    "orders": [
        sa.Column("execution_mode", sa.String(20), nullable=True),
        sa.Column("market_data_source", sa.String(30), nullable=True),
        sa.Column("reference_price", PRICE, nullable=True),
        sa.Column("bid", PRICE, nullable=True),
        sa.Column("ask", PRICE, nullable=True),
        sa.Column("quote_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("slippage_cost", MONEY, nullable=True),
    ],
    "positions": [
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("market_exchange", sa.String(30), nullable=True),
        sa.Column("last_evaluated_candle_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bars_held", sa.Integer(), nullable=True),
        sa.Column("max_holding_bars", sa.Integer(), nullable=True),
        sa.Column("decision_id", sa.String(36), nullable=True),
        sa.Column("slippage_cost", MONEY, nullable=True),
        sa.Column("exit_price", PRICE, nullable=True),
    ],
    "strategies": [
        sa.Column("validation_summary", sa.JSON(), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    ],
}
BACKFILL = (
    # Existing orders keep their venue identity; their data source was not recorded then.
    "UPDATE orders SET execution_mode = CASE WHEN exchange = 'testnet' THEN 'TESTNET' ELSE 'PAPER' END WHERE execution_mode IS NULL",
    "UPDATE orders SET market_data_source = 'NOT_RECORDED' WHERE market_data_source IS NULL",
    "UPDATE orders SET slippage_cost = 0 WHERE slippage_cost IS NULL",
    "UPDATE positions SET bars_held = 0 WHERE bars_held IS NULL",
    "UPDATE positions SET slippage_cost = 0 WHERE slippage_cost IS NULL",
    "UPDATE strategies SET validation_summary = '{}' WHERE validation_summary IS NULL",
)
REQUIRED = {"orders": ("execution_mode", "market_data_source", "slippage_cost"),
            "positions": ("bars_held", "slippage_cost"), "strategies": ("validation_summary",)}
NEW_INDEXES = (
    ("ix_orders_execution_mode", "orders", ["execution_mode"]),
    ("ix_positions_decision_id", "positions", ["decision_id"]),
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns(table: str) -> dict[str, dict]:
    return {column["name"]: column for column in _inspector().get_columns(table)}


def upgrade() -> None:
    for name in NEW_TABLES:
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)
    for table, columns in NEW_COLUMNS.items():
        existing = _columns(table)
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column.copy())
    for statement in BACKFILL:
        op.execute(sa.text(statement))
    types = {table: {column.name: column.type for column in columns} for table, columns in NEW_COLUMNS.items()}
    for table, names in REQUIRED.items():
        current = _columns(table)
        pending = [name for name in names if current[name]["nullable"]]
        if pending:
            with op.batch_alter_table(table) as batch:
                for name in pending:
                    batch.alter_column(name, existing_type=types[table][name], nullable=False)
    for name, table, columns in NEW_INDEXES:
        indexed = {tuple(item.get("column_names") or ()) for item in _inspector().get_indexes(table)}
        if tuple(columns) not in indexed:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for table, columns in NEW_COLUMNS.items():
        removed = {column.name for column in columns}
        for index in _inspector().get_indexes(table):
            if index.get("name") and removed & set(index.get("column_names") or ()):
                op.drop_index(index["name"], table_name=table)
        existing = _columns(table)
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column.name in existing:
                    batch.drop_column(column.name)
    for name in reversed(NEW_TABLES):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
