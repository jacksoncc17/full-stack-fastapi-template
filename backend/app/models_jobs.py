"""
Job-queue models: SGF video render requests, their artifacts, and workers.

Design notes that matter
------------------------
* **Artifacts are stored in Postgres, in a SEPARATE table.** `Artifact` holds
  metadata only (kind, size, hash, dimensions); the bytes live in `ArtifactBlob`.
  Keeping them apart means listing a job's artifacts -- which the dashboard does
  on every poll -- never loads the blobs. Postgres TOASTs and compresses large
  values, so a ~10 MB mp4 is workable, but this is explicitly a first
  implementation: the schema keeps a storage-agnostic shape so moving bytes to
  object storage later means adding a backend, not migrating the schema.

* **The queue is the `job` table, claimed with `FOR UPDATE SKIP LOCKED`.** There
  is no Redis/RabbitMQ in this stack. `claim_job` in crud_jobs.py does the
  locking; the ORDER BY plus the partial index on queued rows makes claiming
  cheap and non-blocking across workers.

* **Leases, not locks.** A claimed job carries `lease_expires_at`. If a worker
  dies, the lease lapses and `reap_expired_leases` returns the job to the queue.
  Every worker write is guarded by the lease token so a worker that wakes up
  after its lease expired cannot overwrite a newer worker's result -- without
  that guard a slow worker silently clobbers a completed job.

* **Workers are not users.** They authenticate with their own API key
  (stored hashed, the plaintext is shown once at registration) and never touch
  the user-facing JWT routes.
"""

import uuid
from datetime import UTC, datetime
from enum import Enum

from sqlalchemy import Column, DateTime, Index, LargeBinary, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


class JobStatus(str, Enum):
    """Job lifecycle.

    QUEUED -> CLAIMED -> RUNNING -> SUCCEEDED | FAILED
    Any of the middle states can fall back to QUEUED when a lease expires.
    CANCELED is terminal and may be requested by the owner at any point before
    SUCCEEDED.
    """

    queued = "queued"
    claimed = "claimed"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


TERMINAL_STATUSES = (JobStatus.succeeded, JobStatus.failed, JobStatus.canceled)


class Worker(SQLModel, table=True):
    """A processing node that may claim jobs.

    `capabilities` is free-form JSON so a worker can advertise what it supports
    (e.g. {"formats": ["mp4", "webp"], "has_katago": true}) without a schema
    change. `api_key_hash` is a hash, never the key itself.
    """

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=128, unique=True, index=True)
    api_key_hash: str
    capabilities: dict | None = Field(default=None, sa_column=Column(Text))
    is_active: bool = True
    last_heartbeat_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class JobBase(SQLModel):
    """Request parameters. These mirror the flags of go_music_video.py so a
    request can be turned into a pipeline invocation without translation."""

    title: str | None = Field(default=None, max_length=255)
    duration_s: float = Field(default=180.0, gt=0, le=480.0)
    bpm: float | None = Field(default=69.0, gt=0, le=400)
    musical_key: str | None = Field(default="D minor", max_length=32)
    time_sig: str | None = Field(default="3", max_length=8)
    seed: str | None = Field(default=None, max_length=32)
    visits: int = Field(default=300, gt=0, le=100000)
    formats: str = Field(default="mp4,webp", max_length=64)
    reference_audio_id: uuid.UUID | None = Field(default=None, foreign_key="artifact.id")


class Job(JobBase, table=True):
    __table_args__ = (
        # the claim query filters on status and orders by created_at
        Index("ix_job_claim", "status", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )

    # input: an SGF is a few KB of text, so it lives inline
    sgf_text: str = Field(sa_column=Column(Text))
    sgf_sha256: str = Field(max_length=64)
    sgf_name: str = Field(max_length=255)

    status: JobStatus = Field(default=JobStatus.queued, index=True)

    # lease / retry bookkeeping
    worker_id: uuid.UUID | None = Field(
        default=None, foreign_key="worker.id", ondelete="SET NULL"
    )
    lease_token: uuid.UUID | None = Field(default=None)
    lease_expires_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)

    # outcome
    error: str | None = Field(default=None, sa_column=Column(Text))
    progress: int = Field(default=0, ge=0, le=100)

    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    started_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    finished_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )

    artifacts: list["Artifact"] = Relationship(
        back_populates="job",
        cascade_delete=True,
        sa_relationship_kwargs={"foreign_keys": "Artifact.job_id"},
    )
    events: list["JobEvent"] = Relationship(
        back_populates="job", cascade_delete=True
    )


class JobCreate(SQLModel):
    """What a client submits. `sgf_text` is validated by the route."""

    sgf_text: str
    sgf_name: str = Field(default="game.sgf", max_length=255)
    title: str | None = Field(default=None, max_length=255)
    duration_s: float = Field(default=180.0, gt=0, le=480.0)
    bpm: float | None = Field(default=69.0, gt=0, le=400)
    musical_key: str | None = Field(default="D minor", max_length=32)
    time_sig: str | None = Field(default="3", max_length=8)
    seed: str | None = Field(default=None, max_length=32)
    visits: int = Field(default=300, gt=0, le=100000)
    formats: str = Field(default="mp4,webp", max_length=64)


class JobPublic(JobBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    sgf_name: str
    status: JobStatus
    progress: int
    attempts: int
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobsPublic(SQLModel):
    data: list[JobPublic]
    count: int


ARTIFACT_KINDS = (
    "mp4",
    "webp",
    "gif",
    "mp3",
    "programme_html",
    "phases_svg",
    "analysis_json",
    "prompt_txt",
    "log",
)


class Artifact(SQLModel, table=True):
    """Metadata for one output file. Deliberately excludes the bytes."""

    __table_args__ = (UniqueConstraint("job_id", "kind", name="uq_artifact_job_kind"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    job_id: uuid.UUID = Field(
        foreign_key="job.id", nullable=False, ondelete="CASCADE", index=True
    )
    kind: str = Field(max_length=32)
    filename: str = Field(max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    bytes: int = Field(default=0)
    sha256: str | None = Field(default=None, max_length=64)
    # storage-agnostic on purpose: today "db", tomorrow "s3"
    storage_backend: str = Field(default="db", max_length=32)
    storage_key: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )

    job: Job | None = Relationship(
        back_populates="artifacts",
        sa_relationship_kwargs={"foreign_keys": "Artifact.job_id"},
    )
    blob: "ArtifactBlob" = Relationship(
        back_populates="artifact", cascade_delete=True
    )


class ArtifactBlob(SQLModel, table=True):
    """The bytes. Separate table so metadata queries never touch them."""

    artifact_id: uuid.UUID = Field(
        foreign_key="artifact.id", primary_key=True, ondelete="CASCADE"
    )
    data: bytes = Field(sa_column=Column(LargeBinary))

    artifact: Artifact | None = Relationship(back_populates="blob")


class ArtifactPublic(SQLModel):
    """What the API returns about an artifact -- never the bytes."""

    id: uuid.UUID
    job_id: uuid.UUID
    kind: str
    filename: str
    mime_type: str
    bytes: int
    sha256: str | None = None
    created_at: datetime | None = None


class JobEvent(SQLModel, table=True):
    """Append-only progress log, which is what drives the dashboard timeline."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    job_id: uuid.UUID = Field(
        foreign_key="job.id", nullable=False, ondelete="CASCADE", index=True
    )
    at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )
    level: str = Field(default="info", max_length=16)
    message: str = Field(sa_column=Column(Text))
    progress: int | None = Field(default=None, ge=0, le=100)

    job: Job | None = Relationship(back_populates="events")


class JobEventPublic(SQLModel):
    id: uuid.UUID
    at: datetime | None = None
    level: str
    message: str
    progress: int | None = None


# ---- worker-facing payloads -------------------------------------------------


class JobClaimRequest(SQLModel):
    worker_name: str = Field(max_length=128)
    lease_seconds: int = Field(default=900, gt=0, le=86400)


class JobClaimed(SQLModel):
    """Everything a worker needs to run one job, and nothing more."""

    job_id: uuid.UUID
    lease_token: uuid.UUID
    lease_expires_at: datetime
    attempts: int
    sgf_text: str
    sgf_name: str
    title: str | None = None
    duration_s: float
    bpm: float | None = None
    musical_key: str | None = None
    time_sig: str | None = None
    seed: str | None = None
    visits: int
    formats: str


class JobProgress(SQLModel):
    lease_token: uuid.UUID
    progress: int = Field(ge=0, le=100)
    message: str | None = Field(default=None, max_length=2000)


class ArtifactUpload(SQLModel):
    """One output file, base64 is NOT used: the route takes multipart."""

    kind: str = Field(max_length=32)
    filename: str = Field(max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=128)


class JobComplete(SQLModel):
    lease_token: uuid.UUID
    success: bool = True
    error: str | None = Field(default=None, max_length=8000)


class WorkerHeartbeat(SQLModel):
    capabilities: dict | None = None
