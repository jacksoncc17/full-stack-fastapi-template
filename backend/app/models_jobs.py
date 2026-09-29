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

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship, SQLModel


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


# Every job belongs to exactly one service, and a worker is dedicated to exactly
# one service. The default keeps the existing SGF-video pipeline working without
# every caller having to name it.
DEFAULT_SERVICE = "sgf_video"


class Service(SQLModel, table=True):
    """A published service contract.

    The BROKER NEVER RUNS SERVICE CODE. It only needs to know the contract so it can
    validate submissions and render a form: this table is what a worker publishes
    about itself at registration, not a copy of the implementation.

    Populated by workers (`POST /worker/services`), so a service exists exactly when
    a worker able to run it has connected. `param_schema` is JSON Schema and drives
    both validation and the frontend form, which is why it belongs here rather than
    being hard-coded per service.
    """

    name: str = Field(primary_key=True, max_length=64)
    title: str = Field(max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text))
    param_schema: dict | None = Field(default=None, sa_column=Column(JSON))
    artifact_kinds: list | None = Field(default=None, sa_column=Column(JSON))
    version: str = Field(default="1", max_length=32)
    updated_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class WorkerService(SQLModel, table=True):
    """Which service a worker serves. One row per worker: a worker is dedicated.

    Deliberately a table rather than a column on `worker` so that allowing a worker
    to serve several services later is a change to the claim query alone, not a
    migration of the worker table.
    """

    worker_id: uuid.UUID = Field(
        foreign_key="worker.id", primary_key=True, ondelete="CASCADE"
    )
    service: str = Field(foreign_key="service.name", max_length=64, index=True)


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
    # JSON, not Text: psycopg cannot adapt a Python dict to a plain text
    # column ("cannot adapt type 'dict'"), which surfaced as a 500 on the
    # worker heartbeat the first time a worker reported its capabilities.
    capabilities: dict | None = Field(default=None, sa_column=Column(JSON))
    is_active: bool = True
    last_heartbeat_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)  # type: ignore
    )
    created_at: datetime | None = Field(
        default_factory=get_datetime_utc,
        sa_type=DateTime(timezone=True),  # type: ignore
    )


class JobBase(SQLModel):
    """Request parameters.

    `service` + `params` are the general form: params is whatever the service's
    `param_schema` describes, and the broker treats it as opaque data it validates
    but does not interpret.

    The fields below are LEGACY: they are the SGF-video pipeline's own parameters,
    kept so existing clients, the generated TypeScript client and the current submit
    form keep working. A new service supplies `params` instead and leaves these at
    their defaults. They are never read by the broker -- only echoed -- so nothing
    has to change here when a service with different parameters is added.
    """

    service: str = Field(default=DEFAULT_SERVICE, max_length=64, index=True)
    params: dict | None = Field(default=None, sa_column=Column(JSON))
    service_version: str | None = Field(default=None, max_length=32)

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
        # the lease reaper filters on status AND lease_expires_at. ix_job_status
        # alone cannot help: status has ~5 distinct values, so the planner would
        # still read every row of that status. This runs on every claim attempt,
        # including while the queue is empty, so without it the reaper is a
        # repeated full scan as the job table grows.
        Index("ix_job_lease_reap", "status", "lease_expires_at"),
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
    """What a client submits.

    Two ways to fill this in, and they coexist on purpose:

    * **new services** set `service` and put everything the service needs in
      `params`, which is validated against that service's published schema
    * **the existing SGF-video client** omits `service`, sets the legacy fields
      below, and the route derives `params` from them

    The second path is why the legacy fields were not deleted: it keeps the current
    API, the generated TypeScript client and the existing submit form working
    without a breaking change.
    """

    service: str = Field(default=DEFAULT_SERVICE, max_length=64)
    params: dict | None = None

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


class ServicePublish(SQLModel):
    """A worker announcing the service it runs.

    `param_schema` is JSON Schema. The broker validates submissions against it and
    the frontend renders a form from it, so it is the single place a service
    describes its own parameters.
    """

    name: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    param_schema: dict | None = None
    artifact_kinds: list[str] | None = None
    version: str = Field(default="1", max_length=32)


class ServicePublic(SQLModel):
    name: str
    title: str
    description: str | None = None
    param_schema: dict | None = None
    artifact_kinds: list | None = None
    version: str
    updated_at: datetime | None = None


class ServicesPublic(SQLModel):
    data: list[ServicePublic]
    count: int


class WorkerHeartbeat(SQLModel):
    capabilities: dict | None = None


# ---- admin-facing payloads --------------------------------------------------


class WorkerRegister(SQLModel):
    """Ask the broker to enrol a new processing node."""

    name: str = Field(min_length=1, max_length=128)
    capabilities: dict | None = None


class WorkerRegistered(SQLModel):
    """Returned ONLY at registration.

    `api_key` is the plaintext key and this is the only time it is ever
    transmitted: the database stores a hash of it, exactly as it does for user
    passwords. If it is lost the worker must be re-registered -- there is no
    "show me the key again" endpoint, by design.
    """

    id: uuid.UUID
    name: str
    api_key: str
    created_at: datetime | None = None


class WorkerPublic(SQLModel):
    """Worker as seen by an admin afterwards -- never includes the key."""

    id: uuid.UUID
    name: str
    capabilities: dict | None = None
    is_active: bool
    last_heartbeat_at: datetime | None = None
    created_at: datetime | None = None


class WorkersPublic(SQLModel):
    data: list[WorkerPublic]
    count: int
