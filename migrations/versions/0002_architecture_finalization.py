"""Architecture finalization entities and boundary fields."""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0002_architecture_finalization"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    _add("research_plans", sa.Column("scope", sa.JSON(), nullable=True))
    _add("research_plans", sa.Column("experiment_plan", sa.JSON(), nullable=True))
    _add("strategy_validation_results", sa.Column("dataset_id", sa.String(36), nullable=True))
    _add("strategy_validation_results", sa.Column("configuration", sa.JSON(), nullable=True))
    _add("decisions", sa.Column("selected_opportunity_id", sa.String(36), nullable=True))
    _add("execution_plans", sa.Column("stop_loss", sa.Float(), nullable=True))
    _add("execution_plans", sa.Column("take_profit", sa.Float(), nullable=True))
    _add("execution_plans", sa.Column("leverage", sa.Float(), nullable=True))
    _add("execution_plans", sa.Column("limit_price", sa.Float(), nullable=True))
    for name in (
        "market_contexts",
        "opportunities",
        "risk_events",
        "position_events",
        "execution_reports",
        "reconciliation_issues",
    ):
        Base.metadata.tables[name].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for name in (
        "reconciliation_issues",
        "execution_reports",
        "position_events",
        "risk_events",
        "opportunities",
        "market_contexts",
    ):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
