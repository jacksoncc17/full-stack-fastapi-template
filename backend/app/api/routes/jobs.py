"""
User-facing job routes: submit a job, watch it, cancel it, fetch results.

Separate from app/api/routes/worker.py on purpose. Everything here is scoped to
`current_user`; superusers may see every job. Nothing here can claim or complete a
job -- that is worker territory.
"""

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.responses import Response
from sqlmodel import col, func, select

from app import crud_jobs as cj
from app import param_schema
from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.models_jobs import (
    DEFAULT_SERVICE,
    JobInput,
    JobInputBlob,
    JobInputPublic,
    Service,
    Artifact,
    ArtifactPublic,
    Job,
    JobCreate,
    JobEvent,
    JobEventPublic,
    JobPublic,
    JobsPublic,
    JobStatus,
    ServicePublic,
    ServicesPublic,
)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# An SGF is a small text file. Refuse anything enormous before it reaches the DB.
MAX_SGF_BYTES = 2 * 1024 * 1024

# A worker heartbeats on every claim, so this window is generous; it only needs to
# be longer than the gap between polls on an idle queue.
OFFLINE_AFTER_MINUTES = 3


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


def _is_bundle(filename: str | None) -> bool:
    """True when a submission's file is a .zip rather than raw SGF text.

    The SGF service takes either a bare SGF or a bundle holding the SGF plus the
    variation diagrams to show after the game. A bundle's bytes are not SGF text --
    they are a zip archive -- so the text check below cannot judge one, and must not
    be applied to it. The service unpacks and parses it properly.
    """
    return bool(filename) and filename.lower().endswith(".zip")


def _check_sgf_submission(service: str, sgf_text: str, filename: str | None) -> None:
    """Run the SGF text check, where it is meaningful.

    ONE place decides this, because the check is reached by more than one path: the
    JSON route (`POST /jobs/`), the generic multipart route (`POST /jobs/submit`, which
    reuses the JSON route's function) and the legacy upload route. Having the rule in
    the reused function and again in its caller is how a bundle got past one check only
    to be rejected by the other, with an error about the file not starting with "(;".
    """
    if service != DEFAULT_SERVICE:
        return
    if _is_bundle(filename):
        return
    _validate_sgf(sgf_text)


def _own_job(session: SessionDep, user: CurrentUser, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.owner_id != user.id and not user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough privileges")
    return job


# The legacy fields, in the order they are turned into `params` for the default
# service. Keeping this mapping in one place means the shim and the schema cannot
# drift apart silently.
LEGACY_PARAM_FIELDS = (
    "duration_s", "bpm", "musical_key", "time_sig", "seed", "visits", "formats",
)


def _params_for(job_in: JobCreate, session: SessionDep) -> dict:
    """Resolve a submission to (service, params) and validate against the schema.

    A service whose schema is published gets real validation at submit time, so a
    bad request is rejected with a useful message instead of sitting in the queue
    until a worker picks it and fails.

    A service with NO published schema is accepted unvalidated. That is deliberate:
    jobs are often submitted before the first worker has ever connected, and
    rejecting them would make the queue unusable at bring-up.
    """
    if job_in.params:
        params = dict(job_in.params)
    else:
        # legacy client: derive params from the flat fields
        params = {f: getattr(job_in, f) for f in LEGACY_PARAM_FIELDS}

    svc = session.get(Service, job_in.service)
    if svc is not None and svc.param_schema:
        try:
            param_schema.validate(instance=params, schema=svc.param_schema)
        except param_schema.SchemaError as e:
            # the SERVICE author's bug, not the submitter's
            raise HTTPException(status_code=500,
                                detail=f"service {job_in.service!r} published an "
                                       f"unsupported schema: {e}")
        except param_schema.ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))
        params = param_schema.apply_defaults(params, svc.param_schema)
    return params


@router.post("/", response_model=JobPublic)
def submit_job(
    *, session: SessionDep, current_user: CurrentUser, job_in: JobCreate
) -> Any:
    """Queue a new job."""
    # SGF validation only applies to the service that consumes an SGF. A new service
    # must not be forced through it, or the plugin point is not real.
    _check_sgf_submission(job_in.service, job_in.sgf_text, job_in.sgf_name)
    params = _params_for(job_in, session)
    svc = session.get(Service, job_in.service)
    job = Job(
        owner_id=current_user.id,
        service=job_in.service,
        params=params,
        service_version=svc.version if svc else None,
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


@router.post("/submit", response_model=JobPublic)
async def submit_job_generic(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    service: str = Form(default=DEFAULT_SERVICE),
    params: str = Form(default="{}"),
    files: list[UploadFile] = File(default=[]),
    file_field: str = Form(default="sgf"),
    file_fields: str = Form(default=""),
) -> Any:
    """Submit a job with optional input files.

    One route for every service: `service` names the service, `params` is its
    parameter JSON (validated against the service's published schema), and `files`
    are the inputs. This is what lets the frontend render any service's form without
    knowing anything about it -- the SGF-specific `/upload` route only exists for the
    legacy client.

    **A service may declare more than one file field.** `file_field` names a single
    field and is what the first client sent, so it still works. `file_fields` is the
    general form: a comma-separated field name per uploaded file, in the same order,
    which is how a service like intonationCheck receives a reference clip, a clip to
    check and a score as three separate inputs. Without this, every file landed on one
    field and any multi-input service was unsubmittable -- it validated (the required
    names were all present in params) and then failed in the worker, where the second
    and third inputs did not exist.

    Files are stored per job and fetched by the worker, rather than inlined in the
    job row, so a large input does not slow down every status query.
    """
    try:
        parsed = json.loads(params or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"params is not valid JSON: {e}")
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="params must be a JSON object")

    field_names = _file_field_names(files, file_field, file_fields)

    # Files are NOT part of the params JSON (the frontend strips them out, because
    # they travel as multipart parts). But a service declares its file field inside
    # param_schema, so if that field is required, validating the raw params would
    # always fail with "missing required property".
    #
    # So the uploaded filenames are injected into params before validation: the
    # values arriving over the wire are satisfied by the parts that arrived with
    # them. The worker still fetches the bytes by name separately.
    for name, upload in zip(field_names, files):
        if not name:
            continue
        existing = parsed.get(name)
        filename = upload.filename or "input"
        # an array field (format: files) takes all of them; a scalar takes the first
        parsed[name] = (existing or []) + [filename] if isinstance(existing, list) \
            else [filename]

    # SGF validation is a property of the SGF service, not of submission, and a .zip
    # bundle is exempt -- see _check_sgf_submission, which owns that rule for every
    # path that reaches it. The text has to be read here rather than left to the inner
    # route because that route only ever receives what this one hands it.
    sgf_text = ""
    if service == DEFAULT_SERVICE and not _is_bundle(
            files[0].filename if files else None):
        for f in files:
            body = (await f.read()).decode("utf-8", errors="replace")
            sgf_text = sgf_text or body
            await f.seek(0)

    job_in = JobCreate(service=service, params=parsed, sgf_text=sgf_text,
                       sgf_name=(files[0].filename if files else "input.txt") or "input.txt")
    job = submit_job(session=session, current_user=current_user, job_in=job_in)

    for name, f in zip(field_names, files):
        data = await f.read()
        _store_input(session=session, job_id=job.id, name=name or file_field,
                     filename=f.filename or "input", data=data,
                     mime_type=f.content_type or "application/octet-stream")
    return job


def _file_field_names(files: list[UploadFile], file_field: str,
                      file_fields: str) -> list[str]:
    """Which param field each uploaded file belongs to.

    `file_fields` is one name per file, comma-separated and positional. Blank entries
    fall back to `file_field`, so an older client that sends only `file_field` keeps
    working unchanged and a newer one can mix the two.

    A count mismatch is refused rather than padded: silently attaching a file to the
    wrong field, or dropping one, produces a job that fails later in the worker with
    a message about a missing input -- which points at the wrong layer.
    """
    if not files:
        return []
    if not file_fields.strip():
        return [file_field] * len(files)

    names = [part.strip() for part in file_fields.split(",")]
    if len(names) != len(files):
        raise HTTPException(
            status_code=422,
            detail=f"file_fields lists {len(names)} field(s) but {len(files)} file(s) "
                   f"were uploaded; they must correspond one to one",
        )
    return [name or file_field for name in names]


def _store_input(*, session: Session, job_id: uuid.UUID, name: str,
                 filename: str, data: bytes, mime_type: str) -> None:
    existing = session.exec(
        select(JobInput).where(JobInput.job_id == job_id, JobInput.name == name)
    ).first()
    if existing is not None:
        existing.filename, existing.bytes = filename, len(data)
        existing.mime_type = mime_type
        session.add(existing)
        blob = session.get(JobInputBlob, existing.id)
        if blob is None:
            session.add(JobInputBlob(input_id=existing.id, data=data))
        else:
            blob.data = data
            session.add(blob)
    else:
        row = JobInput(job_id=job_id, name=name, filename=filename,
                       mime_type=mime_type, bytes=len(data))
        session.add(row)
        session.flush()
        session.add(JobInputBlob(input_id=row.id, data=data))
    session.commit()


@router.get("/{job_id}/inputs", response_model=list[JobInputPublic])
def read_job_inputs(session: SessionDep, current_user: CurrentUser,
                    job_id: uuid.UUID) -> Any:
    _own_job(session, current_user, job_id)
    rows = session.exec(select(JobInput).where(JobInput.job_id == job_id)).all()
    return [JobInputPublic.model_validate(r) for r in rows]


@router.post("/upload", response_model=JobPublic)
async def submit_job_file(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    file: UploadFile,
    service: str = DEFAULT_SERVICE,
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
        service=service,
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


@router.get("/services", response_model=ServicesPublic)
def read_services(session: SessionDep, current_user: CurrentUser) -> Any:
    """Services a job can be submitted to.

    Services appear here once a worker has published them, so an empty list means no
    worker is connected -- which is the same condition the dashboard flags as
    "stalled", and worth showing on a submission form (do not offer a service whose
    worker does not exist).
    """
    svcs = cj.list_services(session=session)
    return ServicesPublic(
        data=[ServicePublic.model_validate(x) for x in svcs], count=len(svcs)
    )


@router.get("/workers-status", response_model=dict)
def workers_status(session: SessionDep, current_user: CurrentUser) -> Any:
    """Is anything actually able to process the queue?

    Deliberately available to any authenticated user, not just admins, because a
    queued job with no worker and a stalled job look identical on the dashboard --
    this is what distinguishes them. It exposes only aggregate liveness: no worker
    names, ids or keys.
    """
    from datetime import UTC, datetime, timedelta

    from sqlmodel import col, select

    from app.models_jobs import Job, Worker

    now = datetime.now(UTC)
    workers = session.exec(select(Worker).where(Worker.is_active)).all()
    fresh = [w for w in workers
             if w.last_heartbeat_at is not None
             and (w.last_heartbeat_at.replace(tzinfo=UTC)
                  if w.last_heartbeat_at.tzinfo is None
                  else w.last_heartbeat_at) > now - timedelta(minutes=OFFLINE_AFTER_MINUTES)]

    pending = session.exec(
        select(func.count()).select_from(Job)
        .where(col(Job.status).in_([JobStatus.queued, JobStatus.claimed,
                                    JobStatus.running]))
    ).one()
    oldest = session.exec(
        select(Job).where(Job.status == JobStatus.queued)
        .order_by(col(Job.created_at)).limit(1)
    ).first()

    return {
        "workers_registered": len(workers),
        "workers_online": len(fresh),
        "offline_after_minutes": OFFLINE_AFTER_MINUTES,
        "pending_jobs": pending,
        "oldest_queued_at": oldest.created_at.isoformat()
        if oldest and oldest.created_at else None,
        # the dashboard's cue: work waiting, nobody to do it
        "stalled": pending > 0 and len(fresh) == 0,
    }


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
    # force: the admin asked explicitly, so do not let the throttle skip it
    return {"reaped": cj.reap_expired_leases(session=session, force=True)}
