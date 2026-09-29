"""add the lease-reap index

Revision ID: b7d1f4c9a2e5
Revises: a66c3a2d72c4
Create Date: 2026-09-29 02:20:00.000000

The lease reaper runs on every claim attempt and filters on status plus
lease_expires_at. `ix_job_status` cannot serve it: status has about five distinct
values, so the planner would read every row of that status. Without this index the
reaper degrades into a repeated full scan of `job` as the table grows.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d1f4c9a2e5"
down_revision = "a66c3a2d72c4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_job_lease_reap", "job", ["status", "lease_expires_at"], unique=False
    )


def downgrade():
    op.drop_index("ix_job_lease_reap", table_name="job")
