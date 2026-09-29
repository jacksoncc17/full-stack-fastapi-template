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
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, func, select

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


# ---- claiming --------------------------------------------------------------


def reap_expired_leases(*, session: Session, now: datetime | None = None) -> int:
    """Return claimed/running jobs whose lease lapsed back to the queue.

    A job that has already used up `max_attempts` is failed rather than requeued,
    so a job that reliably kills its worker cannot be retried forever.
    """
    now = now or get_datetime_utc()
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
    storage_backend: str = "db",
    storage_key: str | None = None,
) -> Artifact:
    """Attach one output file to a job. Idempotent per (job, kind).

    Re-uploading the same kind replaces it, so a retried job that re-sends its
    outputs does not violate the unique constraint or leave two mp4s.
    """
    _load_for_worker(session=session, job_id=job_id, lease_token=lease_token)

    existing = session.exec(
        select(Artifact).where(Artifact.job_id == job_id, Artifact.kind == kind)
    ).first()
    digest = hashlib.sha256(data).hexdigest()

    if existing is not None:
        existing.filename = filename
        existing.mime_type = mime_type
        existing.bytes = len(data)
        existing.sha256 = digest
        existing.storage_backend = storage_backend
        existing.storage_key = storage_key
        session.add(existing)
        artifact = existing
        blob = session.get(ArtifactBlob, existing.id)
        if blob is None:
            session.add(ArtifactBlob(artifact_id=existing.id, data=data))
        else:
            blob.data = data
            session.add(blob)
    else:
        artifact = Artifact(
            job_id=job_id, kind=kind, filename=filename, mime_type=mime_type,
            bytes=len(data), sha256=digest, storage_backend=storage_backend,
            storage_key=storage_key,
        )
        session.add(artifact)
        session.flush()  # need artifact.id for the blob
        session.add(ArtifactBlob(artifact_id=artifact.id, data=data))
    session.commit()
    session.refresh(artifact)
    return artifact


def get_artifact_data(*, session: Session, artifact_id: uuid.UUID) -> bytes | None:
    blob = session.get(ArtifactBlob, artifact_id)
    return blob.data if blob else None


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
