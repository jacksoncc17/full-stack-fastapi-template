"""add services and job routing

Revision ID: c8e2a5b1f3d7
Revises: b7d1f4c9a2e5
Create Date: 2026-09-29 03:40:00.000000

Introduces services so the broker can host more than one kind of job.

* `service` holds a published contract (JSON Schema params + artifact kinds). It is
  written by workers at registration; the broker never runs service code.
* `workerservice` binds a worker to exactly one service, which is what makes the
  claim query filter by service.
* `job.service` / `job.params` / `job.service_version` carry the general form. The
  legacy SGF-video columns stay in place, defaulted, so existing clients keep
  working -- a compatibility shim fills `params` from them on submission.

Existing rows are backfilled to the default service so nothing in an already-running
queue becomes unroutable.
"""

import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

revision = "c8e2a5b1f3d7"
down_revision = "b7d1f4c9a2e5"
branch_labels = None
depends_on = None

DEFAULT_SERVICE = "sgf_video"


def upgrade():
    op.create_table(
        "service",
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("param_schema", sa.JSON(), nullable=True),
        sa.Column("artifact_kinds", sa.JSON(), nullable=True),
        sa.Column("version", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "workerservice",
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("service", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["worker.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service"], ["service.name"]),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index("ix_workerservice_service", "workerservice", ["service"])

    op.add_column(
        "job",
        sa.Column("service", sqlmodel.sql.sqltypes.AutoString(length=64),
                  nullable=False, server_default=DEFAULT_SERVICE),
    )
    op.add_column("job", sa.Column("params", sa.JSON(), nullable=True))
    op.add_column(
        "job",
        sa.Column("service_version", sqlmodel.sql.sqltypes.AutoString(length=32),
                  nullable=True),
    )
    op.create_index("ix_job_service", "job", ["service"])
    # service-aware claiming filters on both, so index them together
    op.create_index("ix_job_service_claim", "job", ["service", "status", "created_at"])

    # seed the default service so existing queued jobs have something to route to,
    # and so a fresh install shows one usable service before any worker connects
    op.execute(
        sa.text(
            "INSERT INTO service (name, title, description, version, updated_at) "
            "VALUES (:n, :t, :d, '1', now()) ON CONFLICT (name) DO NOTHING"
        ).bindparams(
            n=DEFAULT_SERVICE,
            t="SGF video render",
            d="Renders a Go game record to a video scored with generated music.",
        )
    )


def downgrade():
    op.drop_index("ix_job_service_claim", table_name="job")
    op.drop_index("ix_job_service", table_name="job")
    op.drop_column("job", "service_version")
    op.drop_column("job", "params")
    op.drop_column("job", "service")
    op.drop_index("ix_workerservice_service", table_name="workerservice")
    op.drop_table("workerservice")
    op.drop_table("service")
