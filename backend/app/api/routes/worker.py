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
from sqlmodel import Session, select

from app import crud_jobs as cj
from app import param_schema
from app.api.deps import SessionDep
from app.models_jobs import (
    Artifact,
    JobInput,
    JobInputBlob,
    JobInputPublic,
    ServicePublish,
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


def _claimed(session: Session, job) -> JobClaimed:
    """Build the worker's view of a job.

    Params are derived the same way submissions are: a job submitted through the
    legacy flat fields has `params` populated by the submit route, but a row written
    before that shim existed does not, so fall back to the flat fields rather than
    handing the worker an empty dict.
    """
    params = job.params
    if not params:
        params = {f: getattr(job, f) for f in (
            "duration_s", "bpm", "musical_key", "time_sig", "seed", "visits",
            "formats")}
    input_names = list(session.exec(
        select(JobInput.name).where(JobInput.job_id == job.id)
    ).all()) if job.id else []
    return JobClaimed(
        job_id=job.id,
        inputs=input_names,
        lease_token=job.lease_token,
        lease_expires_at=job.lease_expires_at,
        attempts=job.attempts,
        service=job.service,
        params=params,
        payload=job.sgf_text,
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
    return _claimed(session, job)


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
    return _claimed(session, job)


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


@router.post("/services", response_model=dict)
def publish_service(
    session: SessionDep, worker: CurrentWorker, body: ServicePublish
) -> Any:
    """Publish the contract of the service this worker runs, and bind to it.

    Called by a worker at startup. The broker stores the contract for validation and
    for the frontend form, but never runs the service -- the implementation lives in
    the worker. Binding the worker at the same time is what makes routing work: a
    worker claims only jobs for the service it announced.
    """
    if body.param_schema:
        try:
            param_schema.check_schema(body.param_schema)
        except param_schema.SchemaError as e:
            # fail at registration, not at submission: a schema we only partly
            # enforce would make callers believe validation had happened
            raise HTTPException(
                status_code=422,
                detail=f"unsupported param_schema: {e}",
            )
    svc = cj.upsert_service(
        session=session, name=body.name, title=body.title,
        description=body.description, param_schema=body.param_schema,
        artifact_kinds=body.artifact_kinds, version=body.version,
    )
    cj.set_worker_service(session=session, worker_id=worker.id, service=body.name)
    return {"service": svc.name, "version": svc.version, "title": svc.title}


@router.get("/services", response_model=dict)
def my_service(session: SessionDep, worker: CurrentWorker) -> Any:
    """What this worker is bound to -- useful when a worker behaves unexpectedly."""
    return {"worker": worker.name,
            "service": cj.get_worker_service(session=session, worker_id=worker.id)}


@router.post("/heartbeat", response_model=dict)
def heartbeat(
    session: SessionDep, worker: CurrentWorker, body: WorkerHeartbeat | None = None
) -> Any:
    body = body or WorkerHeartbeat()
    cj.touch_worker(session=session, worker=worker, capabilities=body.capabilities)
    return {"worker": worker.name, "ok": True}


@router.get("/jobs/{job_id}/inputs", response_model=list[JobInputPublic])
def list_job_inputs(session: SessionDep, worker: CurrentWorker,
                    job_id: uuid.UUID) -> Any:
    """Input files for a claimed job -- metadata only, so a worker can decide what to
    fetch (and skip re-fetching an input it already has from a retry)."""
    rows = session.exec(select(JobInput).where(JobInput.job_id == job_id)).all()
    return [JobInputPublic.model_validate(r) for r in rows]


@router.get("/jobs/{job_id}/inputs/{name}/data")
def download_job_input(session: SessionDep, worker: CurrentWorker,
                       job_id: uuid.UUID, name: str) -> Response:
    row = session.exec(
        select(JobInput).where(JobInput.job_id == job_id, JobInput.name == name)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No input named {name!r}")
    blob = session.get(JobInputBlob, row.id)
    if blob is None:
        raise HTTPException(status_code=404, detail="Input has no stored bytes")
    return Response(content=blob.data, media_type=row.mime_type,
                    headers={"Content-Disposition":
                             f'attachment; filename="{row.filename}"'})


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
