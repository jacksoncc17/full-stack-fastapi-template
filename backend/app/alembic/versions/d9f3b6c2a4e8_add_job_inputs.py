"""add job inputs

Revision ID: d9f3b6c2a4e8
Revises: c8e2a5b1f3d7
Create Date: 2026-09-29 04:20:00.000000

Input files for a job, separate from the job row.

SGF records are small and stay inline in `job.sgf_text`, which is why that column
exists. A generic service may need a real file, and inlining bytes in the job row
would bloat every status query -- so inputs are rows of their own with the bytes in a
companion table, mirroring how artifacts are stored.
"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision = "d9f3b6c2a4e8"
down_revision = "c8e2a5b1f3d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobinput",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("filename", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("mime_type", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "name", name="uq_jobinput_job_name"),
    )
    op.create_index("ix_jobinput_job_id", "jobinput", ["job_id"])
    op.create_table(
        "jobinputblob",
        sa.Column("input_id", sa.Uuid(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["input_id"], ["jobinput.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("input_id"),
    )


def downgrade():
    op.drop_table("jobinputblob")
    op.drop_index("ix_jobinput_job_id", table_name="jobinput")
    op.drop_table("jobinput")
