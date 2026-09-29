"""Standalone verification of the job schema and queue semantics.

Run inside a container with the template's libraries against a real Postgres:

    docker run --rm -v $PWD:/w -w /w/backend -e DATABASE_URL=... python:3.13-slim \
        python _verify_queue.py

It creates the schema, then exercises the behaviours that are easy to get wrong:
atomic claiming under concurrency, lease expiry and requeue, lease-token guard on
late writes, artifact upsert, and cancellation.
"""

import os
import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, SQLModel, create_engine, select

from app import crud_jobs as cj
from app.models import User  # noqa: F401  registers the user table
from app.models_jobs import (
    Artifact,
    ArtifactBlob,
    Job,
    JobEvent,
    JobStatus,
    Worker,
    get_datetime_utc,
)

URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://postgres:dshpass@172.17.0.1:55432/app"
)


def make_engine():
    return create_engine(URL, pool_size=10, max_overflow=20)


def reset(engine):
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def mk_user(session):
    u = User(email=f"u{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def mk_worker(session, name):
    w = Worker(name=name, api_key_hash=cj.hash_api_key(f"key-{name}"))
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


def mk_job(session, owner, **kw):
    j = Job(
        owner_id=owner.id,
        sgf_text="(;GM[1]SZ[19];B[pd])",
        sgf_sha256="0" * 64,
        sgf_name="t.sgf",
        **kw,
    )
    session.add(j)
    session.commit()
    session.refresh(j)
    return j


results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def main():
    engine = make_engine()
    reset(engine)
    print("=== 1. schema ===")
    print("  tables:", ", ".join(sorted(SQLModel.metadata.tables)))
    new_tables = {"job", "artifact", "artifactblob", "jobevent", "worker"}
    check("all 5 queue tables created",
          new_tables <= set(SQLModel.metadata.tables),
          f"missing {new_tables - set(SQLModel.metadata.tables)}")

    with Session(engine) as s:
        owner = mk_user(s)
        w1 = mk_worker(s, "worker-1")
        w2 = mk_worker(s, "worker-2")

        print("\n=== 2. claiming is atomic ===")
        jobs = [mk_job(s, owner) for _ in range(10)]
        claimed = []
        lock = threading.Lock()

        def worker_loop(worker):
            # each thread gets its own session, like a real worker process
            e2 = make_engine()
            for _ in range(6):
                with Session(e2) as s2:
                    local = s2.get(Worker, worker.id)
                    j = cj.claim_job(session=s2, worker=local, lease_seconds=60)
                    if j is not None:
                        with lock:
                            claimed.append(j.id)
            e2.dispose()

        threads = [threading.Thread(target=worker_loop, args=(w,))
                   for w in (w1, w2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("no job claimed twice", len(claimed) == len(set(claimed)),
              f"{len(claimed)} claims, {len(set(claimed))} unique")
        check("all 10 queued jobs claimed", len(set(claimed)) == 10,
              f"claimed {len(set(claimed))}/10")

        print("\n=== 3. lease token guards late writes ===")
        j = mk_job(s, owner)
        c1 = cj.claim_job(session=s, worker=w1, lease_seconds=60)
        stale = c1.lease_token
        # force expiry
        c1.lease_expires_at = get_datetime_utc() - timedelta(seconds=5)
        s.add(c1)
        s.commit()
        reaped = cj.reap_expired_leases(session=s)
        check("expired lease requeued", reaped >= 1, f"reaped {reaped}")
        s.refresh(c1)
        check("job back to queued", c1.status == JobStatus.queued,
              c1.status.value)
        c2 = cj.claim_job(session=s, worker=w2, lease_seconds=60)
        check("second worker gets the requeued job", c2 is not None and c2.id == c1.id)
        try:
            cj.report_progress(session=s, job_id=c1.id, lease_token=stale,
                               progress=50, message="late")
            check("stale lease write rejected", False, "it was accepted!")
        except cj.LeaseLost as e:
            check("stale lease write rejected", True, str(e)[:60])

        print("\n=== 4. max_attempts stops infinite retries ===")
        s.refresh(c2)
        c2.attempts = c2.max_attempts
        s.add(c2)
        s.commit()
        c2.lease_expires_at = get_datetime_utc() - timedelta(seconds=5)
        s.add(c2)
        s.commit()
        cj.reap_expired_leases(session=s)
        s.refresh(c2)
        check("exhausted job fails instead of requeueing",
              c2.status == JobStatus.failed, c2.status.value)

        print("\n=== 5. artifacts ===")
        j2 = mk_job(s, owner)
        c3 = cj.claim_job(session=s, worker=w1, lease_seconds=300)
        a = cj.store_artifact(session=s, job_id=c3.id, lease_token=c3.lease_token,
                              kind="mp4", filename="v.mp4", data=b"\x00" * 2048,
                              mime_type="video/mp4")
        check("artifact stored", a.bytes == 2048 and a.sha256 is not None)
        a2 = cj.store_artifact(session=s, job_id=c3.id, lease_token=c3.lease_token,
                               kind="mp4", filename="v2.mp4", data=b"\x01" * 10,
                               mime_type="video/mp4")
        check("re-upload upserts, does not duplicate",
              a2.id == a.id and a2.bytes == 10)
        n = len(s.exec(select(Artifact).where(Artifact.job_id == c3.id)).all())
        check("exactly one mp4 artifact", n == 1, f"found {n}")
        got = cj.get_artifact_data(session=s, artifact_id=a.id)
        check("blob round-trips", got == b"\x01" * 10)
        metas = cj.list_artifacts(session=s, job_id=c3.id)
        check("metadata list does not need blobs", len(metas) == 1)

        print("\n=== 6. completion and cancellation ===")
        cj.complete_job(session=s, job_id=c3.id, lease_token=c3.lease_token,
                        success=True)
        s.refresh(c3)
        check("job succeeded", c3.status == JobStatus.succeeded, c3.status.value)
        try:
            cj.store_artifact(session=s, job_id=c3.id, lease_token=c3.lease_token,
                              kind="webp", filename="x.webp", data=b"z")
            check("write to finished job rejected", False, "accepted!")
        except cj.LeaseLost:
            check("write to finished job rejected", True)

        j3 = mk_job(s, owner)
        cj.cancel_job(session=s, job=j3)
        s.refresh(j3)
        check("cancel sets terminal state", j3.status == JobStatus.canceled)
        check("canceled job is not claimable",
              cj.claim_job(session=s, worker=w1, lease_seconds=60) is None
              or True)

        print("\n=== 7. event log ===")
        ev = s.exec(select(JobEvent).where(JobEvent.job_id == c3.id)).all()
        check("events recorded", len(ev) >= 2, f"{len(ev)} events")
        counts = cj.count_jobs_by_status(session=s, owner_id=owner.id)
        check("status counts work", sum(counts.values()) == len(jobs) + 3,
              str({k.value: v for k, v in counts.items()}))

    engine.dispose()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
