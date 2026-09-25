"""Truthful integrations: provider checks, point-in-time macro, venue separation, decision rationale.

Revision ID: 0006_truthful_integrations
Revises: 0005_autonomous_research
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0006_truthful_integrations"
down_revision = "0005_autonomous_research"
branch_labels = None
depends_on = None

NEW_TABLES = ("provider_checks", "macro_observations")
NEW_COLUMNS = {
    "positions": [sa.Column("venue", sa.String(20), nullable=True)],
    "portfolio_snapshots": [sa.Column("venue", sa.String(20), nullable=True)],
    "decisions": [sa.Column("rationale", sa.JSON(), nullable=True)],
    "market_events": [sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
                      sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True)],
    "job_executions": [sa.Column("duration_ms", sa.Integer(), nullable=True)],
}
BACKFILL = (
    "UPDATE positions SET venue = 'paper' WHERE venue IS NULL",
    "UPDATE portfolio_snapshots SET venue = 'paper' WHERE venue IS NULL",
    "UPDATE decisions SET rationale = '{}' WHERE rationale IS NULL",
    # Existing events became knowable when published (or at their event time if no publication time).
    "UPDATE market_events SET available_at = COALESCE(published_at, event_at) WHERE available_at IS NULL",
)
REQUIRED = {"positions": ("venue",), "portfolio_snapshots": ("venue",), "decisions": ("rationale",)}
NEW_INDEXES = (
    ("ix_positions_venue", "positions", ["venue"]),
    ("ix_portfolio_snapshots_venue", "portfolio_snapshots", ["venue"]),
    ("ix_market_events_available_at", "market_events", ["available_at"]),
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
