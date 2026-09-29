"""
Artifact byte storage, behind a single interface.

Why this exists
---------------
Where artifact bytes live is a deployment decision, not a domain one, and the two
answerable options have very different costs:

* **db** -- bytes go into `artifactblob.data` (Postgres `bytea`). Zero extra
  infrastructure, and it works from the first deploy. The price is that every
  backup, replica and restore carries every video: roughly 15 MB per job, so ~150 GB
  at ten thousand jobs.
* **volume** -- bytes go to a directory on a mounted volume, and the database keeps
  only metadata plus a storage key. Removes the bloat, survives container
  recreation (unlike the container's own /tmp), and is shared by all worker
  processes because they share the mount. Still single-host.

Neither is right forever; object storage is the destination. `Artifact` already
carries `storage_backend` and `storage_key`, so switching again means adding one
class here rather than migrating data.

The important property: nothing outside this module knows which backend is in use,
or needs to. Callers store bytes and fetch them back.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Session

from app.models_jobs import Artifact, ArtifactBlob


class StorageError(RuntimeError):
    pass


class StorageBackend:
    name = "abstract"

    def put(self, artifact_id: uuid.UUID, data: bytes) -> str | None:
        """Persist bytes. Returns the storage key, or None when the store keys by
        row id (which is what the db backend does)."""
        raise NotImplementedError

    def get(self, artifact_id: uuid.UUID, key: str | None) -> bytes | None:
        raise NotImplementedError

    def delete(self, artifact_id: uuid.UUID, key: str | None) -> None:
        raise NotImplementedError


class DbBackend(StorageBackend):
    """Bytes in Postgres, in the separate artifactblob table."""

    name = "db"

    def __init__(self, session: Session):
        self.session = session

    def put(self, artifact_id: uuid.UUID, data: bytes) -> str | None:
        blob = self.session.get(ArtifactBlob, artifact_id)
        if blob is None:
            self.session.add(ArtifactBlob(artifact_id=artifact_id, data=data))
        else:
            blob.data = data
            self.session.add(blob)
        return None

    def get(self, artifact_id: uuid.UUID, key: str | None) -> bytes | None:
        blob = self.session.get(ArtifactBlob, artifact_id)
        return blob.data if blob else None

    def delete(self, artifact_id: uuid.UUID, key: str | None) -> None:
        self.session.exec(
            delete(ArtifactBlob).where(ArtifactBlob.artifact_id == artifact_id)
        )


class VolumeBackend(StorageBackend):
    """Bytes in a directory that is expected to be a mounted volume.

    Layout is two levels deep by the first two hex characters of the artifact id:
    a single directory holding hundreds of thousands of entries is slow to list and
    awkward to inspect, and sharding is cheap to do now and painful to retrofit.

    Writes go to a temporary file in the same directory and are then renamed, so a
    crash mid-write cannot leave a truncated file that looks complete. Downloads and
    uploads are different requests, possibly to different worker processes, so a
    partially written artifact is otherwise genuinely reachable.
    """

    name = "volume"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: uuid.UUID) -> Path:
        h = artifact_id.hex
        return self.root / h[:2] / h[2:4] / h

    def _resolve(self, artifact_id: uuid.UUID, key: str | None) -> Path:
        """Locate an artifact.

        `storage_key` is stored ABSOLUTE. An earlier version stored it relative to
        the configured root and rebuilt the path from the current configuration on
        read, which means changing ARTIFACT_DIR silently broke downloads of every
        artifact written before the change -- the row still existed and still said
        "volume", but the bytes were looked for under the wrong root. Storing the
        absolute path makes a read independent of how the process is configured now.

        A relative key is still accepted so rows written by that earlier version
        keep working; it resolves against the configured root, which is the best
        available guess.
        """
        if not key:
            return self._path(artifact_id)
        p = Path(key)
        if not p.is_absolute():
            p = self.root / p
        return p

    def put(self, artifact_id: uuid.UUID, data: bytes) -> str | None:
        dest = self._path(artifact_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)          # atomic within the filesystem
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"could not write {dest}: {e}") from e
        # absolute on purpose -- see _resolve
        return str(dest)

    def get(self, artifact_id: uuid.UUID, key: str | None) -> bytes | None:
        path = self._resolve(artifact_id, key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise StorageError(f"could not read {path}: {e}") from e

    def delete(self, artifact_id: uuid.UUID, key: str | None) -> None:
        path = self._resolve(artifact_id, key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # a failed delete only leaks disk; it must not fail the request that
            # triggered it
            pass


def backend_for(session: Session, artifact_dir: str | None) -> StorageBackend:
    """Pick the backend.

    Defaults to the database so that a deployment which sets nothing behaves exactly
    as before, and so tests and local runs need no volume.
    """
    if artifact_dir:
        return VolumeBackend(Path(artifact_dir))
    return DbBackend(session)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
