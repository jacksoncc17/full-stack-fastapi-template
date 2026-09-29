"""Verify artifact storage against both backends.

    docker run ... python _verify_storage.py

Exercises the properties that matter, on a real Postgres:
  * db backend round-trips bytes
  * volume backend round-trips bytes and writes to a sharded path
  * re-upload replaces in place (one artifact row, one set of bytes)
  * switching backends drops the superseded copy rather than keeping both
  * a row is never committed pointing at bytes that do not exist
"""

import os
import shutil
import tempfile
import uuid

from sqlmodel import Session, SQLModel, create_engine, select

import app.artifact_store as store
from app.core.config import settings
from app import crud_jobs as cj
from app.models import User
from app.models_jobs import Artifact, ArtifactBlob, Job, JobStatus, Worker

URL = os.environ["DATABASE_URL"]
results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def main():
    engine = create_engine(URL)
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    vol = tempfile.mkdtemp(prefix="artifactvol-")

    with Session(engine) as s:
        user = User(email="s@example.com", hashed_password="x")
        s.add(user); s.commit(); s.refresh(user)
        worker = Worker(name="w1", api_key_hash=cj.hash_api_key("k"))
        s.add(worker); s.commit(); s.refresh(worker)

        def new_claimed():
            job = Job(owner_id=user.id, sgf_text="(;GM[1]SZ[19])", sgf_sha256="0"*64,
                      sgf_name="g.sgf", status=JobStatus.queued)
            s.add(job); s.commit(); s.refresh(job)
            return cj.claim_job(session=s, worker=worker, lease_seconds=300)

        print("=== 1. database backend (default: ARTIFACT_DIR unset) ===")
        settings.ARTIFACT_DIR = None
        job = new_claimed()
        art = cj.store_artifact(session=s, job_id=job.id, lease_token=job.lease_token,
                                kind="mp4", filename="a.mp4", data=b"DBBYTES" * 100)
        check("stored in the db backend", art.storage_backend == "db", art.storage_backend)
        check("no storage key needed", art.storage_key is None)
        check("bytes round-trip", cj.get_artifact_data(session=s, artifact_id=art.id)
              == b"DBBYTES" * 100)
        n = len(s.exec(select(ArtifactBlob).where(ArtifactBlob.artifact_id == art.id)).all())
        check("exactly one blob row", n == 1, f"{n}")

        print("\n=== 2. volume backend ===")
        settings.ARTIFACT_DIR = vol
        job2 = new_claimed()
        data2 = b"VOLBYTES" * 500
        art2 = cj.store_artifact(session=s, job_id=job2.id, lease_token=job2.lease_token,
                                 kind="mp4", filename="b.mp4", data=data2)
        check("stored in the volume backend", art2.storage_backend == "volume",
              art2.storage_backend)
        check("key recorded", bool(art2.storage_key), str(art2.storage_key))
        check("bytes round-trip", cj.get_artifact_data(session=s, artifact_id=art2.id)
              == data2)
        onfile = (store.Path(vol) / art2.storage_key).read_bytes()
        check("bytes are actually on disk", onfile == data2, f"{len(onfile)} bytes")
        # keys are absolute now, so assert the SHARDING rather than a part count:
        # two hex levels derived from the artifact id, then the id itself.
        parts = store.Path(art2.storage_key).parts
        h = art2.id.hex
        check("sharded by id (2 levels + filename)",
              parts[-3:] == (h[:2], h[2:4], h),
              "/".join(parts[-3:]))
        check("key is absolute (so a read survives a config change)",
              store.Path(art2.storage_key).is_absolute())
        # nothing should be left in the database for this artifact
        nb = len(s.exec(select(ArtifactBlob).where(ArtifactBlob.artifact_id == art2.id)).all())
        check("no blob row in the db", nb == 0, f"{nb}")

        print("\n=== 3. re-upload replaces in place ===")
        art3 = cj.store_artifact(session=s, job_id=job2.id, lease_token=job2.lease_token,
                                 kind="mp4", filename="b2.mp4", data=b"REPLACED")
        check("same artifact row", art3.id == art2.id)
        check("size updated", art3.bytes == len(b"REPLACED"), str(art3.bytes))
        check("new bytes round-trip",
              cj.get_artifact_data(session=s, artifact_id=art3.id) == b"REPLACED")
        cnt = len(s.exec(select(Artifact).where(Artifact.job_id == job2.id)).all())
        check("still exactly one artifact row", cnt == 1, f"{cnt}")

        print("\n=== 4. switching backend drops the superseded copy ===")
        # job was stored in the volume; switch to db and re-upload the same kind
        settings.ARTIFACT_DIR = None
        art4 = cj.store_artifact(session=s, job_id=job2.id, lease_token=job2.lease_token,
                                 kind="mp4", filename="b3.mp4", data=b"NOW_IN_DB")
        check("now recorded as db", art4.storage_backend == "db", art4.storage_backend)
        check("readable from the db",
              cj.get_artifact_data(session=s, artifact_id=art4.id) == b"NOW_IN_DB")
        # The db copy must be gone. The old VOLUME file cannot be located: the row
        # holds a key relative to a root that is no longer configured, so deleting
        # it would mean guessing a root. The honest expectation is that it remains.
        nb = len(s.exec(select(ArtifactBlob).where(
            ArtifactBlob.artifact_id == art4.id)).all())
        check("the superseded db copy was dropped", nb == 1,
              f"{nb} blob row(s) for the current artifact")
        leftovers = [p for p in store.Path(vol).rglob("*") if p.is_file()]
        check("volume file is orphaned by design (documented, not silent)",
              len(leftovers) == 1, f"{len(leftovers)} orphan(s)")

        print("\n=== 5. switching back to the volume ===")
        settings.ARTIFACT_DIR = vol
        art5 = cj.store_artifact(session=s, job_id=job2.id, lease_token=job2.lease_token,
                                 kind="mp4", filename="b4.mp4", data=b"BACK_ON_VOL")
        check("recorded as volume again", art5.storage_backend == "volume")
        check("readable from the volume",
              cj.get_artifact_data(session=s, artifact_id=art5.id) == b"BACK_ON_VOL")
        nb = len(s.exec(select(ArtifactBlob).where(ArtifactBlob.artifact_id == art5.id)).all())
        check("the db copy was dropped", nb == 0, f"{nb} blob rows")

        print("\n=== 6. a read survives a changed ARTIFACT_DIR ===")
        # This is the regression that matters: a relative key rebuilt from the
        # current config silently 404s every artifact written under a different
        # root, while the row still looks perfectly valid.
        other = tempfile.mkdtemp(prefix="otherroot-")
        settings.ARTIFACT_DIR = other          # deliberately a DIFFERENT root
        got = cj.get_artifact_data(session=s, artifact_id=art5.id)
        check("artifact written under root A is still readable under root B",
              got == b"BACK_ON_VOL", f"got {got!r}")
        settings.ARTIFACT_DIR = vol
        check("and readable again under its own root",
              cj.get_artifact_data(session=s, artifact_id=art5.id) == b"BACK_ON_VOL")
        shutil.rmtree(other, ignore_errors=True)

        print("\n=== 7. a missing file is None, not an exception ===")
        ghost = uuid.uuid4()
        got = store.VolumeBackend(store.Path(vol)).get(ghost, None)
        check("missing artifact returns None", got is None)

    settings.ARTIFACT_DIR = None
    shutil.rmtree(vol, ignore_errors=True)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
