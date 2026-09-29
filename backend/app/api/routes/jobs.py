"""
User-facing job routes: submit a job, watch it, cancel it, fetch results.

Separate from app/api/routes/worker.py on purpose. Everything here is scoped to
`current_user`; superusers may see every job. Nothing here can claim or complete a
job -- that is worker territory.
"""

import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlmodel import col, func, select

from app import crud_jobs as cj
from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.models_jobs import (
    Artifact,
    ArtifactPublic,
    Job,
    JobCreate,
    JobEvent,
    JobEventPublic,
    JobPublic,
    JobsPublic,
    JobStatus,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# An SGF is a small text file. Refuse anything enormous before it reaches the DB.
MAX_SGF_BYTES = 2 * 1024 * 1024


def _validate_sgf(text: str) -> None:
    """Cheap structural check. This is not a full SGF parser -- the pipeline does
    the real parsing -- but it catches the common mistakes (wrong file, binary
    garbage, truncated download) before a job is queued and a worker spends
    minutes on it.
    """
    stripped = text.lstrip()
    if not stripped.startswith("(;"):
        raise HTTPException(
            status_code=400,
            detail="Not an SGF: expected the file to start with '(;'",
        )
    if "SZ[" not in text:
        raise HTTPException(
            status_code=400, detail="Not an SGF: no SZ[...] board-size property"
        )
    if text.count("(") != text.count(")"):
        raise HTTPException(
            status_code=400,
            detail="Malformed SGF: unbalanced parentheses",
        )
    if len(text) > MAX_SGF_BYTES:
        raise HTTPException(status_code=413, detail="SGF is too large")


def _own_job(session: SessionDep, user: CurrentUser, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.owner_id != user.id and not user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough privileges")
    return job


@router.post("/", response_model=JobPublic)
def submit_job(
    *, session: SessionDep, current_user: CurrentUser, job_in: JobCreate
) -> Any:
    """Queue a new render request."""
    _validate_sgf(job_in.sgf_text)
    job = Job(
        owner_id=current_user.id,
        sgf_text=job_in.sgf_text,
        sgf_sha256=hashlib.sha256(job_in.sgf_text.encode()).hexdigest(),
        sgf_name=job_in.sgf_name,
        title=job_in.title,
        duration_s=job_in.duration_s,
        bpm=job_in.bpm,
        musical_key=job_in.musical_key,
        time_sig=job_in.time_sig,
        seed=job_in.seed,
        visits=job_in.visits,
        formats=job_in.formats,
        status=JobStatus.queued,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    session.add(
        JobEvent(job_id=job.id, level="info", message="job queued", progress=0)
    )
    session.commit()
    return JobPublic.model_validate(job)


@router.post("/upload", response_model=JobPublic)
async def submit_job_file(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    file: UploadFile,
    title: str | None = None,
    duration_s: float = 180.0,
    bpm: float | None = 69.0,
    musical_key: str | None = "D minor",
    time_sig: str | None = "3",
    seed: str | None = None,
    visits: int = 300,
    formats: str = "mp4,webp",
) -> Any:
    """Submit by uploading an .sgf file directly from a browser."""
    raw = await file.read()
    if len(raw) > MAX_SGF_BYTES:
        raise HTTPException(status_code=413, detail="SGF is too large")
    text = raw.decode("utf-8", errors="replace")
    job_in = JobCreate(
        sgf_text=text,
        sgf_name=file.filename or "game.sgf",
        title=title, duration_s=duration_s, bpm=bpm, musical_key=musical_key,
        time_sig=time_sig, seed=seed, visits=visits, formats=formats,
    )
    return submit_job(
        session=session, current_user=current_user, job_in=job_in
    )


@router.get("/", response_model=JobsPublic)
def read_jobs(
    session: SessionDep, current_user: CurrentUser,
    status: JobStatus | None = None, skip: int = 0, limit: int = 100,
) -> Any:
    """List jobs. Users see their own; superusers see everything."""
    conditions = []
    if not current_user.is_superuser:
        conditions.append(Job.owner_id == current_user.id)
    if status is not None:
        conditions.append(Job.status == status)

    count_stmt = select(func.count()).select_from(Job)
    list_stmt = select(Job)
    for c in conditions:
        count_stmt = count_stmt.where(c)
        list_stmt = list_stmt.where(c)

    count = session.exec(count_stmt).one()
    jobs = session.exec(
        list_stmt.order_by(col(Job.created_at).desc()).offset(skip).limit(limit)
    ).all()
    return JobsPublic(
        data=[JobPublic.model_validate(j) for j in jobs], count=count
    )


@router.get("/summary", response_model=dict)
def jobs_summary(session: SessionDep, current_user: CurrentUser) -> Any:
    """Counts per status -- the numbers the dashboard header needs, without
    pulling every row."""
    owner = None if current_user.is_superuser else current_user.id
    counts = cj.count_jobs_by_status(session=session, owner_id=owner)
    return {
        "counts": {s.value: counts.get(s, 0) for s in JobStatus},
        "total": sum(counts.values()),
    }


@router.get("/{job_id}", response_model=JobPublic)
def read_job(
    session: SessionDep, current_user: CurrentUser, job_id: uuid.UUID
) -> Any:
    return JobPublic.model_validate(_own_job(session, current_user, job_id))


@router.get("/{job_id}/events", response_model=list[JobEventPublic])
def read_job_events(
    session: SessionDep, current_user: CurrentUser, job_id: uuid.UUID
) -> Any:
    _own_job(session, current_user, job_id)
    stmt = (
        select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .order_by(col(JobEvent.at))
    )
    return [JobEventPublic.model_validate(e) for e in session.exec(stmt).all()]


@router.get("/{job_id}/artifacts", response_model=list[ArtifactPublic])
def read_job_artifacts(
    session: SessionDep, current_user: CurrentUser, job_id: uuid.UUID
) -> Any:
    _own_job(session, current_user, job_id)
    return [
        ArtifactPublic.model_validate(a)
        for a in cj.list_artifacts(session=session, job_id=job_id)
    ]


@router.get("/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    session: SessionDep, current_user: CurrentUser, job_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> Response:
    """Download one artifact. Ownership is enforced through the job."""
    _own_job(session, current_user, job_id)
    art = session.get(Artifact, artifact_id)
    if art is None or art.job_id != job_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    data = cj.get_artifact_data(session=session, artifact_id=artifact_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Artifact has no stored bytes")
    return Response(
        content=data,
        media_type=art.mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{art.filename}"'
        },
    )


@router.post("/{job_id}/cancel", response_model=JobPublic)
def cancel_job(
    session: SessionDep, current_user: CurrentUser, job_id: uuid.UUID
) -> Any:
    """Cancel a job. A job already running keeps its lease until it expires --
    there is no channel to interrupt a worker -- but the lease guard rejects the
    worker's final write, so the result is discarded rather than silently kept."""
    job = _own_job(session, current_user, job_id)
    if job.status in (JobStatus.succeeded,):
        raise HTTPException(
            status_code=400, detail="Job already succeeded; nothing to cancel"
        )
    return JobPublic.model_validate(cj.cancel_job(session=session, job=job))


@router.post("/admin/reap", response_model=dict)
def reap(
    session: SessionDep,
    _superuser: Annotated[Any, Depends(get_current_active_superuser)],
) -> Any:
    """Force a lease sweep. Normally runs opportunistically on every claim."""
    return {"reaped": cj.reap_expired_leases(session=session)}
