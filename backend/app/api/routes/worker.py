"""
Worker-facing routes: claim, progress, upload, complete.

These are deliberately separate from the user routes and use a different
credential. A worker authenticates with an API key in the `X-Worker-Key` header;
it never sees a user JWT, and a user JWT cannot claim jobs. Mixing the two would
mean any logged-in user could drain the queue or forge results.

The lease token comes back from /claim and must accompany every subsequent write.
That is what makes a slow worker's late write a clean 409 instead of a silent
overwrite of another worker's completed result.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from sqlmodel import Session

from app import crud_jobs as cj
from app.api.deps import SessionDep
from app.models_jobs import (
    Artifact,
    ArtifactPublic,
    Job,
    JobClaimed,
    JobClaimRequest,
    JobComplete,
    JobEventPublic,
    JobProgress,
    Worker,
    WorkerHeartbeat,
)

router = APIRouter(prefix="/worker", tags=["worker"])


def get_current_worker(
    session: SessionDep,
    x_worker_key: Annotated[str | None, Header(alias="X-Worker-Key")] = None,
) -> Worker:
    """Authenticate a worker by API key."""
    if not x_worker_key:
        raise HTTPException(
            status_code=401, detail="Missing X-Worker-Key header"
        )
    worker = cj.get_worker_by_key(session=session, api_key=x_worker_key)
    if worker is None:
        raise HTTPException(status_code=403, detail="Invalid or inactive worker key")
    return worker


CurrentWorker = Annotated[Worker, Depends(get_current_worker)]


@router.post("/claim", response_model=JobClaimed | None)
def claim(
    session: SessionDep, worker: CurrentWorker, req: JobClaimRequest | None = None
) -> Any:
    """Take the oldest queued job, or return null when the queue is empty.

    Returns 200 with a null body rather than 204 so a polling worker has one
    response shape to handle.
    """
    req = req or JobClaimRequest(worker_name=worker.name)
    cj.touch_worker(session=session, worker=worker)
    job = cj.claim_job(
        session=session, worker=worker, lease_seconds=req.lease_seconds
    )
    if job is None:
        return None
    return JobClaimed(
        job_id=job.id,
        lease_token=job.lease_token,  # type: ignore[arg-type]
        lease_expires_at=job.lease_expires_at,  # type: ignore[arg-type]
        attempts=job.attempts,
        sgf_text=job.sgf_text,
        sgf_name=job.sgf_name,
        title=job.title,
        duration_s=job.duration_s,
        bpm=job.bpm,
        musical_key=job.musical_key,
        time_sig=job.time_sig,
        seed=job.seed,
        visits=job.visits,
        formats=job.formats,
    )


@router.post("/jobs/{job_id}/progress", response_model=JobEventPublic)
def progress(
    session: SessionDep, worker: CurrentWorker, job_id: uuid.UUID,
    body: JobProgress,
) -> Any:
    try:
        job = cj.report_progress(
            session=session, job_id=job_id, lease_token=body.lease_token,
            progress=body.progress, message=body.message,
        )
    except cj.LeaseLost as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JobEventPublic(
        id=uuid.uuid4(), at=job.finished_at, level="info",
        message=body.message or f"progress {body.progress}%",
        progress=body.progress,
    )


@router.post("/jobs/{job_id}/renew", response_model=JobClaimed)
def renew(
    session: SessionDep, worker: CurrentWorker, job_id: uuid.UUID,
    lease_token: uuid.UUID, lease_seconds: int = 900,
) -> Any:
    """Extend a lease for a long job. Video renders take minutes."""
    try:
        job = cj.renew_lease(
            session=session, job_id=job_id, lease_token=lease_token,
            lease_seconds=lease_seconds,
        )
    except cj.LeaseLost as e:
        raise HTTPException(status_code=409, detail=str(e))
    return JobClaimed(
        job_id=job.id, lease_token=job.lease_token,  # type: ignore[arg-type]
        lease_expires_at=job.lease_expires_at,  # type: ignore[arg-type]
        attempts=job.attempts, sgf_text=job.sgf_text, sgf_name=job.sgf_name,
        title=job.title, duration_s=job.duration_s, bpm=job.bpm,
        musical_key=job.musical_key, time_sig=job.time_sig, seed=job.seed,
        visits=job.visits, formats=job.formats,
    )


@router.post("/jobs/{job_id}/artifacts", response_model=ArtifactPublic)
async def upload_artifact(
    session: SessionDep, worker: CurrentWorker, job_id: uuid.UUID,
    lease_token: Annotated[uuid.UUID, Form()],
    kind: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    filename: Annotated[str | None, Form()] = None,
    mime_type: Annotated[str, Form()] = "application/octet-stream",
) -> Any:
    """Attach one output file. Re-uploading a kind replaces it.

    The bytes go to Postgres for now (see models_jobs.py for why that is a
    first implementation and not the end state).
    """
    data = await file.read()
    try:
        art = cj.store_artifact(
            session=session, job_id=job_id, lease_token=lease_token, kind=kind,
            filename=filename or file.filename or f"{kind}.bin", data=data,
            mime_type=mime_type or file.content_type
            or "application/octet-stream",
        )
    except cj.LeaseLost as e:
        raise HTTPException(status_code=409, detail=str(e))
    return ArtifactPublic.model_validate(art)


@router.post("/jobs/{job_id}/complete", response_model=dict)
def complete(
    session: SessionDep, worker: CurrentWorker, job_id: uuid.UUID,
    body: JobComplete,
) -> Any:
    try:
        job = cj.complete_job(
            session=session, job_id=job_id, lease_token=body.lease_token,
            success=body.success, error=body.error,
        )
    except cj.LeaseLost as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"job_id": str(job.id), "status": job.status.value}


@router.post("/heartbeat", response_model=dict)
def heartbeat(
    session: SessionDep, worker: CurrentWorker, body: WorkerHeartbeat | None = None
) -> Any:
    body = body or WorkerHeartbeat()
    cj.touch_worker(session=session, worker=worker, capabilities=body.capabilities)
    return {"worker": worker.name, "ok": True}


@router.get("/jobs/{job_id}/artifacts", response_model=list[ArtifactPublic])
def list_job_artifacts(
    session: SessionDep, worker: CurrentWorker, job_id: uuid.UUID
) -> Any:
    """Metadata only -- lets a worker check what it already uploaded before
    re-uploading after a retry."""
    return [
        ArtifactPublic.model_validate(a)
        for a in cj.list_artifacts(session=session, job_id=job_id)
    ]


@router.get("/artifacts/{artifact_id}/data")
def download_artifact_data(
    session: SessionDep, worker: CurrentWorker, artifact_id: uuid.UUID
) -> Response:
    art = session.get(Artifact, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    data = cj.get_artifact_data(session=session, artifact_id=artifact_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Artifact has no stored bytes")
    return Response(content=data, media_type=art.mime_type)
