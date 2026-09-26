"""Persisted paper account initial capital (fixed at creation; never re-derived from configuration).

Revision ID: 0008_paper_account_capital
Revises: 0007_rest_paper_loop
"""

from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0008_paper_account_capital"
down_revision = "0007_rest_paper_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.tables["paper_accounts"].create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.tables["paper_accounts"].drop(bind=op.get_bind(), checkfirst=True)
