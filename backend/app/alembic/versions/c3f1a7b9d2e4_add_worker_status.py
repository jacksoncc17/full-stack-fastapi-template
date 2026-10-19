"""add worker status

Revision ID: c3f1a7b9d2e4
Revises: d9f3b6c2a4e8
Create Date: 2026-10-19 00:00:00.000000

A second JSON column on `worker`: what the node says it is *doing* (its heartbeat
report) as opposed to what it *can* do (`capabilities`, declared once at registration).

Splitting them is what stops a heartbeat from overwriting a declaration -- before this,
the heartbeat wrote the same column, so an admin's capabilities survived at most one
beat and the admin page showed `{"processed": 3}` under a heading about capabilities.

The add is compatible in both directions: an older API build ignores the column, and a
row written before the migration simply has NULL status, which the admin page renders as
a dash.
"""

import sqlalchemy as sa
from alembic import op

revision = "c3f1a7b9d2e4"
down_revision = "d9f3b6c2a4e8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("worker", sa.Column("status", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("worker", "status")
