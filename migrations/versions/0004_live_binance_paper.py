"""Persistent live-data paper-trading sessions.

Revision ID: 0004_live_binance_paper
Revises: 0003_precision_backfill_hardening
"""

from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0004_live_binance_paper"
down_revision = "0003_precision_backfill_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["live_paper_sessions"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["live_paper_sessions"].drop(bind=op.get_bind(), checkfirst=True)
