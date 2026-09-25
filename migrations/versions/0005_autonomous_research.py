"""Autonomous research foundation: universe, regimes, intelligence, AI, memory, learning, safety.

Revision ID: 0005_autonomous_research
Revises: 0004_live_binance_paper

Existing rows are preserved. New lifecycle columns receive explicit backfill values so
older records remain readable; financial columns use the same fixed-precision types.
"""

import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0005_autonomous_research"
down_revision = "0004_live_binance_paper"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(30, 12)

NEW_TABLES = (
    "asset_eligibility",
    "market_regimes",
    "ai_calls",
    "ai_artifacts",
    "trade_memory",
    "post_trade_analyses",
    "knowledge_entries",
    "counterfactuals",
    "strategy_health",
    "discrepancy_reports",
    "safety_controls",
)

NEW_COLUMNS: dict[str, list[sa.Column]] = {
    "hypotheses": [
        sa.Column("stage", sa.String(30), nullable=True),
        sa.Column("origin", sa.String(30), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("market_conditions", sa.JSON(), nullable=True),
        sa.Column("assets", sa.JSON(), nullable=True),
        sa.Column("timeframes", sa.JSON(), nullable=True),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("assumptions", sa.JSON(), nullable=True),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("strategy_id", sa.String(36), nullable=True),
        sa.Column("parent_strategy_id", sa.String(36), nullable=True),
        sa.Column("ai_artifact_id", sa.String(36), nullable=True),
        sa.Column("stage_history", sa.JSON(), nullable=True),
        sa.Column("trials", sa.Integer(), nullable=True),
    ],
    "strategies": [
        sa.Column("origin", sa.String(30), nullable=True),
        sa.Column("parent_strategy_id", sa.String(36), nullable=True),
        sa.Column("hypothesis_id", sa.String(36), nullable=True),
        sa.Column("health_status", sa.String(30), nullable=True),
        sa.Column("health_details", sa.JSON(), nullable=True),
    ],
    "market_events": [
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("external_id", sa.String(200), nullable=True),
        sa.Column("dedup_hash", sa.String(64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_status", sa.String(30), nullable=True),
        sa.Column("relevance", sa.Float(), nullable=True),
        sa.Column("expected_value", sa.String(50), nullable=True),
        sa.Column("actual_value", sa.String(50), nullable=True),
        sa.Column("previous_value", sa.String(50), nullable=True),
        sa.Column("surprise", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("country", sa.String(20), nullable=True),
        sa.Column("raw_reference", sa.JSON(), nullable=True),
        sa.Column("processing_status", sa.String(30), nullable=True),
    ],
    "decisions": [
        sa.Column("exchange", sa.String(30), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("lineage", sa.JSON(), nullable=True),
    ],
    "positions": [
        sa.Column("highest_price", PRICE, nullable=True),
        sa.Column("lowest_price", PRICE, nullable=True),
    ],
    "market_contexts": [
        sa.Column("exchange", sa.String(30), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("market_regime", sa.String(30), nullable=True),
        sa.Column("regime_confidence", sa.Float(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
    ],
    "opportunities": [
        sa.Column("source", sa.String(20), nullable=True),
        sa.Column("exchange", sa.String(30), nullable=True),
        sa.Column("timeframe", sa.String(10), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=True),
        sa.Column("regime", sa.String(30), nullable=True),
        sa.Column("dedup_key", sa.String(120), nullable=True),
        sa.Column("rank_score", sa.Float(), nullable=True),
        sa.Column("rank_breakdown", sa.JSON(), nullable=True),
        sa.Column("event_ids", sa.JSON(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observations", sa.Integer(), nullable=True),
    ],
}

BACKFILL = (
    "UPDATE hypotheses SET stage = 'IDEA' WHERE stage IS NULL",
    "UPDATE hypotheses SET origin = 'operator' WHERE origin IS NULL",
    "UPDATE hypotheses SET description = '' WHERE description IS NULL",
    "UPDATE hypotheses SET decision_reason = '' WHERE decision_reason IS NULL",
    "UPDATE hypotheses SET trials = 0 WHERE trials IS NULL",
    "UPDATE hypotheses SET market_conditions = '{}' WHERE market_conditions IS NULL",
    "UPDATE hypotheses SET variables = '{}' WHERE variables IS NULL",
    "UPDATE hypotheses SET spec = '{}' WHERE spec IS NULL",
    "UPDATE hypotheses SET assets = '[]' WHERE assets IS NULL",
    "UPDATE hypotheses SET timeframes = '[]' WHERE timeframes IS NULL",
    "UPDATE hypotheses SET assumptions = '[]' WHERE assumptions IS NULL",
    "UPDATE hypotheses SET stage_history = '[]' WHERE stage_history IS NULL",
    "UPDATE strategies SET origin = 'operator' WHERE origin IS NULL",
    "UPDATE strategies SET health_status = 'INSUFFICIENT_DATA' WHERE health_status IS NULL",
    "UPDATE strategies SET health_details = '{}' WHERE health_details IS NULL",
    "UPDATE market_events SET provider = 'manual' WHERE provider IS NULL",
    "UPDATE market_events SET verification_status = 'OPERATOR_ENTERED' WHERE verification_status IS NULL",
    "UPDATE market_events SET processing_status = 'NORMALIZED' WHERE processing_status IS NULL",
    "UPDATE market_events SET relevance = 0 WHERE relevance IS NULL",
    "UPDATE market_events SET raw_reference = '{}' WHERE raw_reference IS NULL",
    "UPDATE decisions SET lineage = '{}' WHERE lineage IS NULL",
    "UPDATE market_contexts SET features = '{}' WHERE features IS NULL",
    "UPDATE opportunities SET source = 'decision' WHERE source IS NULL",
    "UPDATE opportunities SET signals = '[]' WHERE signals IS NULL",
    "UPDATE opportunities SET event_ids = '[]' WHERE event_ids IS NULL",
    "UPDATE opportunities SET rank_breakdown = '{}' WHERE rank_breakdown IS NULL",
    "UPDATE opportunities SET rank_score = 0 WHERE rank_score IS NULL",
    "UPDATE opportunities SET observations = 1 WHERE observations IS NULL",
)

NEW_INDEXES = (
    ("ix_hypotheses_stage", "hypotheses", ["stage"]),
    ("ix_hypotheses_origin", "hypotheses", ["origin"]),
    ("ix_hypotheses_strategy_id", "hypotheses", ["strategy_id"]),
    ("ix_hypotheses_parent_strategy_id", "hypotheses", ["parent_strategy_id"]),
    ("ix_hypotheses_ai_artifact_id", "hypotheses", ["ai_artifact_id"]),
    ("ix_strategies_health_status", "strategies", ["health_status"]),
    ("ix_strategies_parent_strategy_id", "strategies", ["parent_strategy_id"]),
    ("ix_strategies_hypothesis_id", "strategies", ["hypothesis_id"]),
    ("ix_market_events_provider", "market_events", ["provider"]),
    ("ix_market_events_published_at", "market_events", ["published_at"]),
    ("ix_market_events_verification_status", "market_events", ["verification_status"]),
    ("ix_market_events_processing_status", "market_events", ["processing_status"]),
    ("ix_decisions_timeframe", "decisions", ["timeframe"]),
    ("ix_market_contexts_exchange", "market_contexts", ["exchange"]),
    ("ix_market_contexts_market_regime", "market_contexts", ["market_regime"]),
    ("ix_market_contexts_timeframe", "market_contexts", ["timeframe"]),
    ("ix_opportunities_source", "opportunities", ["source"]),
    ("ix_opportunities_dedup_key", "opportunities", ["dedup_key"]),
)
# Columns that are required once existing rows have been backfilled.
REQUIRED = {
    "hypotheses": ("stage", "origin", "description", "market_conditions", "assets", "timeframes", "variables",
                   "assumptions", "spec", "decision_reason", "stage_history", "trials"),
    "strategies": ("origin", "health_status", "health_details"),
    "market_events": ("provider", "verification_status", "relevance", "raw_reference", "processing_status"),
    "decisions": ("lineage",),
    "market_contexts": ("features",),
    "opportunities": ("source", "signals", "rank_score", "rank_breakdown", "event_ids", "observations"),
}


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table)}


def _indexed_columns(table: str) -> set[tuple[str, ...]]:
    inspector = _inspector()
    found = {tuple(item.get("column_names") or ()) for item in inspector.get_indexes(table)}
    found |= {tuple(item.get("column_names") or ()) for item in inspector.get_unique_constraints(table)}
    return found


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
        current = {column["name"]: column["nullable"] for column in _inspector().get_columns(table)}
        pending = [name for name in names if current.get(name)]
        relax = table == "opportunities" and not current.get("strategy_version_id", True)
        unique = table == "market_events" and ("dedup_hash",) not in _indexed_columns(table)
        if pending or relax or unique:
            with op.batch_alter_table(table) as batch:
                for name in pending:
                    batch.alter_column(name, existing_type=types[table][name], nullable=False)
                if relax:
                    batch.alter_column("strategy_version_id", existing_type=sa.String(36), nullable=True)
                if unique:
                    batch.create_unique_constraint("uq_market_events_dedup_hash", ["dedup_hash"])
    for name, table, columns in NEW_INDEXES:
        if tuple(columns) not in _indexed_columns(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    # Scanner opportunities have no strategy version and cannot exist in the 0004 schema.
    op.execute(sa.text("DELETE FROM opportunities WHERE strategy_version_id IS NULL"))
    with op.batch_alter_table("opportunities") as batch:
        batch.alter_column("strategy_version_id", existing_type=sa.String(36), nullable=False)
    for table, columns in NEW_COLUMNS.items():
        removed = {column.name for column in columns}
        # Drop every index touching a removed column (named by this migration or by create_all).
        for index in _inspector().get_indexes(table):
            if index.get("name") and removed & set(index.get("column_names") or ()):
                op.drop_index(index["name"], table_name=table)
        existing = _columns(table)
        named_unique = {item["name"] for item in _inspector().get_unique_constraints(table) if item.get("name")}
        with op.batch_alter_table(table) as batch:
            if "uq_market_events_dedup_hash" in named_unique:
                batch.drop_constraint("uq_market_events_dedup_hash", type_="unique")
            for column in columns:
                if column.name in existing:
                    batch.drop_column(column.name)
    for name in reversed(NEW_TABLES):
        Base.metadata.tables[name].drop(bind=op.get_bind(), checkfirst=True)
