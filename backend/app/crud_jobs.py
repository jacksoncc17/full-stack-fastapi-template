"""
Queue operations: claiming, leasing, progress and completion.

The locking lives here because it is the one part of this system that is easy to
get subtly wrong, and the failure modes are silent: a job processed twice, or a
finished job overwritten by a worker that woke up too late.

Postgres is the queue. `claim_job` uses `FOR UPDATE SKIP LOCKED`, which lets N
workers poll the same table concurrently without blocking each other or
double-claiming: each transaction locks a different row, or skips to the next.

Every worker write is guarded by `lease_token`. That guard is not belt-and-braces
-- without it this happens: worker A stalls (GC, network, a stuck engine), its
lease expires, worker B claims and completes the job, then A finishes and writes
its result over the top. The token makes A's late write a no-op with a clear
error instead.
"""

import hashlib
import logging
import secrets
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, func, select

from app import artifact_store

logger = logging.getLogger(__name__)
from app.core.config import settings
from app.models_jobs import (
    TERMINAL_STATUSES,
    Artifact,
    ArtifactBlob,
    Job,
    JobEvent,
    JobStatus,
    Worker,
)


class LeaseLost(Exception):
    """Raised when a worker writes with a lease that is no longer current."""


def get_datetime_utc() -> datetime:
    return datetime.now(UTC)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Postgres returns tz-aware values, but a naive value can arrive from a
    model built in-process; comparison would then raise TypeError."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---- workers ---------------------------------------------------------------


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def get_worker_by_key(*, session: Session, api_key: str) -> Worker | None:
    digest = hash_api_key(api_key)
    statement = select(Worker).where(Worker.api_key_hash == digest)
    worker = session.exec(statement).first()
    if worker and not worker.is_active:
        return None
    return worker


def touch_worker(
    *, session: Session, worker: Worker, capabilities: dict | None = None
) -> Worker:
    worker.last_heartbeat_at = get_datetime_utc()
    if capabilities is not None:
        worker.capabilities = capabilities
    session.add(worker)
    session.commit()
    session.refresh(worker)
    return worker


def register_worker(
    *, session: Session, name: str, capabilities: dict | None = None
) -> tuple[Worker, str]:
    """Enrol a worker and return (worker, plaintext_api_key).

    The plaintext key is returned exactly once and never stored: only its SHA-256
    digest goes in the database, the same posture as a password hash. That is why
    there is no endpoint to retrieve a key later -- losing it means re-registering.

    Re-registering an existing name ROTATES the key rather than failing, because a
    lost key is the common reason to come back here. The worker row keeps its id so
    historical jobs still point at the right node.
    """
    api_key = secrets.token_urlsafe(32)
    digest = hash_api_key(api_key)

    worker = session.exec(select(Worker).where(Worker.name == name)).first()
    if worker is None:
        worker = Worker(name=name, api_key_hash=digest, capabilities=capabilities)
    else:
        worker.api_key_hash = digest          # rotate
        worker.is_active = True
        if capabilities is not None:
            worker.capabilities = capabilities
    session.add(worker)
    session.commit()
    session.refresh(worker)
    return worker, api_key


def list_workers(*, session: Session) -> list[Worker]:
    return list(session.exec(select(Worker).order_by(col(Worker.name))).all())


def set_worker_active(*, session: Session, worker: Worker, active: bool) -> Worker:
    """Enable/disable a worker.

    Disabling does not touch its in-flight job: the lease still has to expire
    before another worker may take over, which is what keeps a disabled-but-busy
    node from being raced.
    """
    worker.is_active = active
    session.add(worker)
    session.commit()
    session.refresh(worker)
    return worker


# ---- claiming --------------------------------------------------------------


# The reaper runs on every claim attempt, and an idle worker polls every few
# seconds, so without throttling it would scan the job table continuously for
# nothing. Expiry does not need second-level precision: a job waits a little
# longer before being retried, which is harmless, whereas scanning on every poll
# is not. Module-level because the cost being avoided is per-process.
_LAST_REAP: datetime | None = None
_REAP_INTERVAL_SECONDS = 15


def reap_expired_leases(
    *, session: Session, now: datetime | None = None, force: bool = False
) -> int:
    """Return claimed/running jobs whose lease lapsed back to the queue.

    A job that has already used up `max_attempts` is failed rather than requeued,
    so a job that reliably kills its worker cannot be retried forever.
    """
    global _LAST_REAP
    now = now or get_datetime_utc()
    if not force:
        if _LAST_REAP is not None and \
                (now - _LAST_REAP).total_seconds() < _REAP_INTERVAL_SECONDS:
            return 0
    _LAST_REAP = now
    statement = (
        select(Job)
        .where(col(Job.status).in_([JobStatus.claimed, JobStatus.running]))
        .where(Job.lease_expires_at.is_not(None))
        .where(Job.lease_expires_at < now)
        .with_for_update(skip_locked=True)
    )
    reaped = 0
    for job in session.exec(statement).all():
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.failed
            job.error = (
                f"lease expired after {job.attempts} attempt(s); "
                f"max_attempts={job.max_attempts} reached"
            )
            job.finished_at = now
            session.add(
                JobEvent(
                    job_id=job.id,
                    level="error",
                    message=job.error,
                    progress=job.progress,
                )
            )
        else:
            job.status = JobStatus.queued
            job.worker_id = None
            job.lease_token = None
            job.lease_expires_at = None
            session.add(
                JobEvent(
                    job_id=job.id,
                    level="warn",
                    message="lease expired; returned to the queue",
                    progress=job.progress,
                )
            )
        session.add(job)
        reaped += 1
    if reaped:
        session.commit()
    return reaped


def claim_job(
    *, session: Session, worker: Worker, lease_seconds: int = 900
) -> Job | None:
    """Atomically take the oldest queued job, or return None if there is none."""
    now = get_datetime_utc()
    reap_expired_leases(session=session, now=now)

    statement = (
        select(Job)
        .where(Job.status == JobStatus.queued)
        .order_by(col(Job.created_at))
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.exec(statement).first()
    if job is None:
        return None

    job.status = JobStatus.claimed
    job.worker_id = worker.id
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.attempts += 1
    job.started_at = job.started_at or now
    session.add(job)
    session.add(
        JobEvent(
            job_id=job.id,
            level="info",
            message=f"claimed by {worker.name} (attempt {job.attempts})",
            progress=job.progress,
        )
    )
    session.commit()
    session.refresh(job)
    return job


def _load_for_worker(
    *, session: Session, job_id: uuid.UUID, lease_token: uuid.UUID
) -> Job:
    """Fetch a job and verify the caller still holds its lease."""
    job = session.get(Job, job_id)
    if job is None:
        raise LeaseLost(f"job {job_id} not found")
    if job.lease_token != lease_token:
        raise LeaseLost(
            "lease token does not match: this worker's lease was superseded "
            "(a newer worker owns the job)"
        )
    if job.status in TERMINAL_STATUSES:
        raise LeaseLost(f"job is already {job.status.value}")
    expires = _as_aware(job.lease_expires_at)
    if expires is not None and expires < get_datetime_utc():
        raise LeaseLost("lease expired")
    return job


def renew_lease(
    *, session: Session, job_id: uuid.UUID, lease_token: uuid.UUID,
    lease_seconds: int = 900,
) -> Job:
    job = _load_for_worker(session=session, job_id=job_id, lease_token=lease_token)
    job.lease_expires_at = get_datetime_utc() + timedelta(seconds=lease_seconds)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def report_progress(
    *, session: Session, job_id: uuid.UUID, lease_token: uuid.UUID,
    progress: int, message: str | None = None,
) -> Job:
    job = _load_for_worker(session=session, job_id=job_id, lease_token=lease_token)
    job.status = JobStatus.running
    job.progress = max(job.progress, progress)
    # a progress ping is also a liveness signal, so push the lease out
    job.lease_expires_at = get_datetime_utc() + timedelta(minutes=15)
    session.add(job)
    if message:
        session.add(
            JobEvent(
                job_id=job.id, level="info", message=message, progress=progress
            )
        )
    session.commit()
    session.refresh(job)
    return job


# ---- artifacts -------------------------------------------------------------


def store_artifact(
    *,
    session: Session,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    kind: str,
    filename: str,
    data: bytes,
    mime_type: str = "application/octet-stream",
) -> Artifact:
    """Attach one output file to a job. Idempotent per (job, kind).

    Re-uploading the same kind replaces it, so a retried job does not violate the
    unique constraint or leave two mp4s behind.

    Replacement also has to clean up the copy it supersedes, including when the
    configured backend changed between attempts. Without that, a job first stored in
    the database and later re-uploaded to the volume would keep its bytes twice, and
    the database copy would never be freed.
    """
    _load_for_worker(session=session, job_id=job_id, lease_token=lease_token)

    store = artifact_store.backend_for(session, settings.ARTIFACT_DIR)
    sha = artifact_store.digest(data)
    existing = session.exec(
        select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == kind)
    ).first()

    if existing is None:
        artifact = Artifact(
            job_id=job_id, kind=kind, filename=filename, mime_type=mime_type,
            bytes=len(data), sha256=sha, storage_backend=store.name,
        )
        session.add(artifact)
        session.flush()                     # the key is derived from artifact.id
        # write the bytes BEFORE committing the row, so a row is never visible
        # pointing at bytes that do not exist yet
        artifact.storage_key = store.put(artifact.id, data)
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
        return artifact

    old_backend, old_key = existing.storage_backend, existing.storage_key
    key = store.put(existing.id, data)       # write first, then repoint the row

    existing.filename = filename
    existing.mime_type = mime_type
    existing.bytes = len(data)
    existing.sha256 = sha
    existing.storage_backend = store.name
    existing.storage_key = key
    session.add(existing)
    session.commit()
    session.refresh(existing)

    if old_backend != store.name:
        # The superseded copy must go, or a job re-uploaded after a storage change
        # keeps its bytes twice.
        #
        # The db copy is always reachable, so it is always dropped. A volume copy is
        # NOT: the row records a key relative to a root that is no longer configured,
        # and that root is not recoverable from the row. Rather than guess a root --
        # which would mean deleting a path derived from a relative key against
        # something like "/" -- the file is left behind and this is logged.
        #
        # Practical consequence: switching ARTIFACT_DIR in either direction while
        # jobs are mid-flight can orphan volume files. They are wasted disk, not
        # corruption, and a deployment that settles on one backend never hits this.
        if old_backend == "db":
            artifact_store.DbBackend(session).delete(existing.id, old_key)
            session.commit()
        else:
            logger.warning(
                "artifact %s moved from volume to %s; the previous volume copy at "
                "key %r is no longer reachable and was not deleted",
                existing.id, store.name, old_key,
            )
    return existing


def get_artifact_data(*, session: Session, artifact_id: uuid.UUID) -> bytes | None:
    """Read artifact bytes from whichever backend holds them.

    The backend is read from the row, not from configuration: an artifact written
    before a storage change must still be downloadable, so the row is the source of
    truth about where its bytes are.
    """
    art = session.get(Artifact, artifact_id)
    if art is None:
        return None
    if art.storage_backend == "volume":
        # the key is absolute, so this works regardless of the current ARTIFACT_DIR
        root = Path(settings.ARTIFACT_DIR) if settings.ARTIFACT_DIR else Path("/")
        return artifact_store.VolumeBackend(root).get(artifact_id, art.storage_key)
    return artifact_store.DbBackend(session).get(artifact_id, art.storage_key)


def list_artifacts(*, session: Session, job_id: uuid.UUID) -> list[Artifact]:
    statement = (
        select(Artifact).where(Artifact.job_id == job_id).order_by(col(Artifact.kind))
    )
    return list(session.exec(statement).all())


# ---- completion ------------------------------------------------------------


def complete_job(
    *, session: Session, job_id: uuid.UUID, lease_token: uuid.UUID,
    success: bool = True, error: str | None = None,
) -> Job:
    job = _load_for_worker(session=session, job_id=job_id, lease_token=lease_token)
    job.status = JobStatus.succeeded if success else JobStatus.failed
    job.error = None if success else error
    job.finished_at = get_datetime_utc()
    job.progress = 100 if success else job.progress
    job.lease_expires_at = None
    session.add(job)
    session.add(
        JobEvent(
            job_id=job.id,
            level="info" if success else "error",
            message="completed successfully" if success else f"failed: {error}",
            progress=job.progress,
        )
    )
    session.commit()
    session.refresh(job)
    return job


def cancel_job(*, session: Session, job: Job) -> Job:
    """Owner-requested cancellation.

    A job already running on a worker cannot be stopped from here -- there is no
    channel to it -- so it is marked canceled and the worker's next write is
    rejected by the lease/terminal-status guard, which is the point of that guard.
    """
    if job.status in TERMINAL_STATUSES:
        return job
    job.status = JobStatus.canceled
    job.finished_at = get_datetime_utc()
    job.lease_token = None
    job.lease_expires_at = None
    session.add(job)
    session.add(
        JobEvent(job_id=job.id, level="warn", message="canceled by owner",
                 progress=job.progress)
    )
    session.commit()
    session.refresh(job)
    return job


def count_jobs_by_status(*, session: Session, owner_id: uuid.UUID | None = None):
    statement = select(Job.status, func.count()).group_by(Job.status)  # type: ignore
    if owner_id is not None:
        statement = statement.where(Job.owner_id == owner_id)
    return {status: count for status, count in session.exec(statement).all()}
