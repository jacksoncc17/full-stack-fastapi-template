"""
Admin routes for managing workers: register, list, enable/disable.

Separate from both the user routes and the worker routes, and gated on
`is_superuser`. Registering a worker mints a credential that can claim and complete
arbitrary jobs, so it is deliberately not something an ordinary user can do --
that would let anyone drain the queue or inject results.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app import crud_jobs as cj
from app.api.deps import SessionDep, get_current_active_superuser
from app.models import User
from app.models_jobs import (
    Worker,
    WorkerPublic,
    WorkerRegister,
    WorkerRegistered,
    WorkersPublic,
)

router = APIRouter(prefix="/admin/workers", tags=["admin"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.post("/", response_model=WorkerRegistered)
def register_worker(
    session: SessionDep, _admin: SuperUser, body: WorkerRegister
) -> Any:
    """Enrol a worker and hand back its API key.

    The key is returned once and only once -- the database keeps a hash. If it is
    lost, register the same name again to rotate it; the worker keeps its id so its
    job history stays attributable.
    """
    worker, api_key = cj.register_worker(
        session=session, name=body.name, capabilities=body.capabilities
    )
    return WorkerRegistered(
        id=worker.id, name=worker.name, api_key=api_key,
        created_at=worker.created_at,
    )


@router.get("/list", response_model=dict)
def read_workers_detail(session: SessionDep, _admin: SuperUser) -> Any:
    """Workers with liveness information for the admin view.

    The plain list endpoint returns the public shape (no heartbeat), which is not
    enough to tell a healthy worker from a dead one.
    """
    from app.api.routes.jobs import OFFLINE_AFTER_MINUTES

    now = datetime.now(UTC)
    out = []
    for w in cj.list_workers(session=session):
        hb = w.last_heartbeat_at
        if hb is not None and hb.tzinfo is None:
            hb = hb.replace(tzinfo=UTC)
        online = bool(w.is_active and hb
                      and hb > now - timedelta(minutes=OFFLINE_AFTER_MINUTES))
        out.append({
            "id": str(w.id), "name": w.name, "is_active": w.is_active,
            "capabilities": w.capabilities,
            "last_heartbeat_at": hb.isoformat() if hb else None,
            "seconds_since_heartbeat": int((now - hb).total_seconds()) if hb else None,
            "online": online,
        })
    return {"data": out, "count": len(out),
            "offline_after_minutes": OFFLINE_AFTER_MINUTES}


@router.get("/", response_model=WorkersPublic)
def read_workers(session: SessionDep, _admin: SuperUser) -> Any:
    workers = cj.list_workers(session=session)
    return WorkersPublic(
        data=[WorkerPublic.model_validate(w) for w in workers],
        count=len(workers),
    )


@router.post("/{worker_id}/active", response_model=WorkerPublic)
def set_active(
    session: SessionDep, _admin: SuperUser, worker_id: uuid.UUID, active: bool
) -> Any:
    """Enable or disable a worker.

    Disabling stops it claiming new jobs but does NOT abort the one it holds: the
    lease has to expire first, which is what prevents a second worker racing a
    node that is still writing.
    """
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    return WorkerPublic.model_validate(
        cj.set_worker_active(session=session, worker=worker, active=active)
    )


@router.get("/{worker_id}/jobs", response_model=dict)
def worker_jobs(
    session: SessionDep, _admin: SuperUser, worker_id: uuid.UUID,
    limit: int = 50,
) -> Any:
    """Recent jobs claimed by a worker -- useful when one node misbehaves."""
    from sqlmodel import col, select

    from app.models_jobs import Job

    worker = session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    stmt = (
        select(Job)
        .where(Job.worker_id == worker_id)
        .order_by(col(Job.created_at).desc())
        .limit(limit)
    )
    jobs = session.exec(stmt).all()
    return {
        "worker": worker.name,
        "count": len(jobs),
        "jobs": [
            {"id": str(j.id), "status": j.status.value, "attempts": j.attempts,
             "title": j.title, "created_at": j.created_at.isoformat()
             if j.created_at else None}
            for j in jobs
        ],
    }
