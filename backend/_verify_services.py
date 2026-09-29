"""Verify service routing and the parameter schema path.

    docker run ... python _verify_services.py

The property that matters: a worker dedicated to one service claims ONLY that
service's jobs. Before this existed, every worker claimed every job, so a worker
for the wrong service would take work it cannot run.
"""

import os
import uuid

from sqlmodel import Session, SQLModel, create_engine, select

from app import crud_jobs as cj
from app import param_schema as ps
from app.models import User
from app.models_jobs import (
    DEFAULT_SERVICE,
    Artifact,
    Job,
    JobStatus,
    Service,
    Worker,
    WorkerService,
)

URL = os.environ["DATABASE_URL"]
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def main():
    engine = create_engine(URL)
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        user = User(email="s@example.com", hashed_password="x")
        s.add(user); s.commit(); s.refresh(user)

        def mk_worker(name, service=None):
            w = Worker(name=name, api_key_hash=cj.hash_api_key(f"k-{name}"))
            s.add(w); s.commit(); s.refresh(w)
            if service:
                cj.set_worker_service(session=s, worker_id=w.id, service=service)
            return w

        def mk_job(service, **params):
            j = Job(owner_id=user.id, sgf_text="(;GM[1]SZ[19])",
                    sgf_sha256="0" * 64, sgf_name="g.sgf", service=service,
                    params=params or None, status=JobStatus.queued)
            s.add(j); s.commit(); s.refresh(j)
            return j

        print("=== 1. services are published with a contract ===")
        cj.upsert_service(session=s, name="sgf_video", title="SGF video render",
                          param_schema={"type": "object",
                                        "properties": {"duration_s": {"type": "number",
                                                                      "default": 180}},
                                        "additionalProperties": False},
                          artifact_kinds=["mp4", "webp"], version="1")
        cj.upsert_service(session=s, name="thumbnail", title="Board thumbnail",
                          param_schema={"type": "object",
                                        "properties": {"size": {"type": "integer",
                                                                "minimum": 16,
                                                                "maximum": 2048}},
                                        "required": ["size"],
                                        "additionalProperties": False},
                          artifact_kinds=["png"], version="2")
        svcs = {x.name: x for x in cj.list_services(session=s)}
        check("two services registered", len(svcs) == 2, ", ".join(sorted(svcs)))
        check("schemas stored", svcs["thumbnail"].param_schema is not None)
        check("artifact kinds stored",
              svcs["thumbnail"].artifact_kinds == ["png"])

        print("\n=== 2. a worker is bound to one service ===")
        w_video = mk_worker("video-1", "sgf_video")
        w_thumb = mk_worker("thumb-1", "thumbnail")
        w_none = mk_worker("unassigned-1")
        check("video worker bound",
              cj.get_worker_service(session=s, worker_id=w_video.id) == "sgf_video")
        check("unassigned worker has no binding",
              cj.get_worker_service(session=s, worker_id=w_none.id) is None)

        print("\n=== 3. THE POINT: routing keeps workers apart ===")
        for i in range(3):
            mk_job("sgf_video", duration_s=180)
        for i in range(2):
            mk_job("thumbnail", size=512)

        # claim one of each, counting those, then drain what is left -- otherwise
        # the totals below miss the two claimed here
        first_video = cj.claim_job(session=s, worker=w_video, lease_seconds=60)
        check("video worker claimed a video job",
              first_video is not None and first_video.service == "sgf_video",
              first_video.service if first_video else "none")
        first_thumb = cj.claim_job(session=s, worker=w_thumb, lease_seconds=60)
        check("thumbnail worker claimed a thumbnail job",
              first_thumb is not None and first_thumb.service == "thumbnail",
              first_thumb.service if first_thumb else "none")

        # drain the thumbnails; the thumbnail worker must never get a video job
        seen_by_thumb = ["thumbnail"]      # the one claimed above
        while True:
            j = cj.claim_job(session=s, worker=w_thumb, lease_seconds=60)
            if j is None:
                break
            seen_by_thumb.append(j.service)
        check("thumbnail worker never touched a video job",
              set(seen_by_thumb) <= {"thumbnail"}, str(seen_by_thumb))

        seen_by_video = ["sgf_video"]      # the one claimed above
        while True:
            j = cj.claim_job(session=s, worker=w_video, lease_seconds=60)
            if j is None:
                break
            seen_by_video.append(j.service)
        check("video worker never touched a thumbnail job",
              set(seen_by_video) <= {"sgf_video"}, str(seen_by_video))
        check("between them every job was claimed exactly once",
              len(seen_by_thumb) + len(seen_by_video) == 5,
              f"thumb={len(seen_by_thumb)} video={len(seen_by_video)}")

        print("\n=== 4. an unassigned worker falls back to the default service ===")
        mk_job("sgf_video")
        j = cj.claim_job(session=s, worker=w_none, lease_seconds=60)
        check("unassigned worker claims the default service",
              j is not None and j.service == DEFAULT_SERVICE,
              j.service if j else "none")
        check("and nothing else", cj.claim_job(session=s, worker=w_none,
                                              lease_seconds=60) is None)

        print("\n=== 5. a worker with no matching service idles rather than misfires ===")
        mk_job("thumbnail", size=256)
        check("video worker declines a thumbnail job",
              cj.claim_job(session=s, worker=w_video, lease_seconds=60) is None)

        print("\n=== 6. params round-trip as opaque JSON ===")
        # step 5 left a thumbnail job QUEUED, and claim is FIFO -- drain it first or
        # this asserts against the wrong job (which is exactly what it caught)
        leftover = cj.claim_job(session=s, worker=w_thumb, lease_seconds=60)
        mk_job("thumbnail", size=1024, watermark="x")
        j = cj.claim_job(session=s, worker=w_thumb, lease_seconds=60)
        check("service-specific params survive the round trip",
              j is not None and j.params == {"size": 1024, "watermark": "x"},
              str(j.params if j else None))

        print("\n=== 7. legacy jobs default to the SGF service ===")
        legacy = Job(owner_id=user.id, sgf_text="(;GM[1])", sgf_sha256="0"*64,
                     sgf_name="legacy.sgf", status=JobStatus.queued)
        s.add(legacy); s.commit(); s.refresh(legacy)
        check("a job created without a service gets the default",
              legacy.service == DEFAULT_SERVICE, legacy.service)
        check("and is claimable by the default worker",
              cj.claim_job(session=s, worker=w_video, lease_seconds=60) is not None)

    print("\n=== 8. the parameter schema validator ===")
    sch = {"type": "object",
           "properties": {"duration_s": {"type": "number", "minimum": 1,
                                         "maximum": 480, "default": 180},
                          "name": {"type": "string", "pattern": "^[a-z]+$"}},
           "required": ["duration_s"],
           "additionalProperties": False}
    try:
        ps.validate(instance={"duration_s": 60}, schema=sch)
        check("valid params accepted", True)
    except Exception as e:
        check("valid params accepted", False, str(e))
    for label, bad in (("missing required", {}),
                       ("wrong type", {"duration_s": "60"}),
                       ("out of range", {"duration_s": 999}),
                       ("unknown property", {"duration_s": 1, "nope": 1}),
                       ("pattern mismatch", {"duration_s": 1, "name": "ABC"})):
        try:
            ps.validate(instance=bad, schema=sch)
            check(f"rejects {label}", False, "accepted!")
        except ps.ValidationError as e:
            check(f"rejects {label}", True, e.message)
    check("defaults are applied",
          ps.apply_defaults({}, sch) == {"duration_s": 180})
    try:
        ps.check_schema({"$ref": "#/definitions/x"})
        check("refuses a $ref schema rather than under-validating", False)
    except ps.SchemaError as e:
        check("refuses a $ref schema rather than under-validating", True,
              str(e)[:48])

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
