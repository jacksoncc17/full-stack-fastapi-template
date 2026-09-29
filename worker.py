#!/usr/bin/env python3
"""
Worker loop: claim SGF video jobs from the broker and run the pipeline.

This is the queue-facing half of the system. The pipeline half already exists in
the toolkit (go_music_video.py); this process claims a job, materialises the SGF,
invokes the pipeline, uploads the artifacts, and reports completion.

Design notes
------------
* **Standard library only.** The pipeline runs under a different interpreter and
  dependency set (the toolkit needs Pillow and ffmpeg; the broker needs SQLModel
  and Python 3.14), so this worker deliberately needs nothing installed. That
  means the broker's dependency tree can never break the worker.

* **The lease is renewed on a background thread.** A render takes minutes and the
  lease is what stops a second worker taking the job. Renewal failing is fatal:
  it means another worker owns the job now, so uploading would be rejected anyway
  and the work should stop rather than waste GPU/CPU.

* **Artifacts are uploaded per file, not batched.** A job that fails halfway
  leaves the successful artifacts in place, so a human can see partial output and
  a retry only re-uploads what it re-ran. store_artifact upserts by (job, kind).

* **The SGF filename matters.** go_music_video.py derives its output directory and
  file names from the SGF stem, so the worker writes the SGF under its original
  name rather than a generic temp name.

Usage
-----
    export WORKER_KEY=...
    python3 worker.py --broker http://127.0.0.1:8000/api/v1 \
        --toolkit /path/to/selfCommentary --once

    # see the queue plumbing without waiting on a real render
    python3 worker.py --broker ... --once --fake-pipeline
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# artifact kind -> how to recognise the file the pipeline produced
KIND_BY_SUFFIX = {
    ".mp4": "mp4",
    ".webp": "webp",
    ".gif": "gif",
    ".mp3": "mp3",
    ".html": "programme_html",
    ".svg": "phases_svg",
}

STOP = threading.Event()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BrokerError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class LeaseLost(BrokerError):
    """The broker rejected a write because this worker no longer owns the job."""


class Broker:
    """Thin client for the worker-facing API."""

    def __init__(self, base: str, api_key: str, timeout: int = 120):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method, path, payload=None, raw=None, content_type=None):
        url = f"{self.base}{path}"
        data = None
        headers = {"X-Worker-Key": self.api_key}
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "application/octet-stream"
        elif payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 409:
                raise LeaseLost(e.code, detail)
            raise BrokerError(e.code, detail)

    def claim(self, worker_name: str, lease_seconds: int):
        return self._request(
            "POST", "/worker/claim",
            {"worker_name": worker_name, "lease_seconds": lease_seconds},
        )

    def heartbeat(self, capabilities=None):
        return self._request("POST", "/worker/heartbeat",
                             {"capabilities": capabilities or {}})

    def renew(self, job_id, token, lease_seconds):
        return self._request("POST", f"/worker/jobs/{job_id}/renew"
                             f"?lease_token={token}&lease_seconds={lease_seconds}")

    def progress(self, job_id, token, pct, message):
        return self._request("POST", f"/worker/jobs/{job_id}/progress",
                             {"lease_token": token, "progress": pct,
                              "message": message})

    def complete(self, job_id, token, success=True, error=None):
        return self._request("POST", f"/worker/jobs/{job_id}/complete",
                             {"lease_token": token, "success": success,
                              "error": error})

    def upload(self, job_id, token, kind, filename, data, mime):
        """multipart/form-data, built by hand to stay dependency-free."""
        boundary = f"----dshworker{uuid.uuid4().hex}"
        parts = []
        for name, value in (("lease_token", token), ("kind", kind),
                            ("filename", filename), ("mime_type", mime)):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)
        return self._request(
            "POST", f"/worker/jobs/{job_id}/artifacts", raw=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )


class LeaseKeeper(threading.Thread):
    """Renews the lease and posts heartbeats while a long render runs.

    Failure is not silently ignored: if the lease is gone, another worker owns the
    job, so the caller must stop. `lost` carries the reason.
    """

    def __init__(self, broker, job_id, token, lease_seconds, interval=300):
        super().__init__(daemon=True)
        self.broker = broker
        self.job_id = job_id
        self.token = token
        self.lease_seconds = lease_seconds
        self.interval = max(30, min(interval, lease_seconds // 2))
        self.lost: Exception | None = None
        self.renewals = 0

    def run(self):
        while not STOP.wait(self.interval):
            try:
                self.broker.renew(self.job_id, self.token, self.lease_seconds)
                self.renewals += 1
                log(f"  lease renewed ({self.renewals})")
            except Exception as e:  # noqa: BLE001 - any failure means stop
                self.lost = e
                log(f"  LEASE LOST: {e}")
                return


def run_real_pipeline(toolkit: Path, sgf: Path, outdir: Path, job: dict,
                      extra: list[str]) -> tuple[int, str]:
    """Invoke go_music_video.py for one job."""
    cmd = [
        sys.executable, str(toolkit / "go_music_video.py"), str(sgf),
        "--outdir", str(outdir),
        "--duration", str(job["duration_s"]),
        "--visits", str(job["visits"]),
        "--formats", job["formats"] or "mp4,webp",
    ]
    if job.get("bpm") is not None:
        cmd += ["--bpm", str(job["bpm"])]
    if job.get("musical_key"):
        cmd += ["--key", job["musical_key"]]
    if job.get("time_sig"):
        cmd += ["--time-sig", job["time_sig"]]
    if job.get("seed"):
        cmd += ["--seed", str(job["seed"])]
    if job.get("title"):
        cmd += ["--title", job["title"]]
    cmd += extra

    log(f"  $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    tail = "\n".join((proc.stdout or "").splitlines()[-20:])
    if proc.returncode != 0:
        err = (proc.stderr or tail)[-3000:]
        return proc.returncode, err
    return 0, tail


def run_fake_pipeline(outdir: Path) -> tuple[int, str]:
    """Produce plausible artifacts without spending minutes rendering.

    Used to exercise the queue plumbing on its own. The bytes are deliberately not
    valid media -- the point is the claim/upload/complete path, not the video.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "fake.mp4").write_bytes(b"\x00" * 4096)
    (outdir / "fake.webp").write_bytes(b"\x01" * 1024)
    (outdir / "fake.mp3").write_bytes(b"\x02" * 2048)
    (outdir / "programme.html").write_text("<html>fake programme</html>")
    return 0, "fake pipeline produced 4 artifacts"


def collect_artifacts(outdir: Path):
    """(kind, path) for every uploadable file the pipeline left behind."""
    found = []
    for p in sorted(outdir.rglob("*")):
        if not p.is_file():
            continue
        if p.name == "pipeline.log":
            continue
        kind = KIND_BY_SUFFIX.get(p.suffix.lower())
        if kind is None:
            continue
        # a job's main video is named after the SGF; anything .mp4 counts
        found.append((kind, p))
    return found


def process_one(broker: Broker, toolkit: Path | None, claimed: dict,
                lease_seconds: int, fake: bool, extra: list[str]) -> bool:
    job_id = claimed["job_id"]
    token = claimed["lease_token"]
    log(f"claimed job {job_id} (attempt {claimed['attempts']}) "
        f"dur={claimed['duration_s']}s formats={claimed['formats']}")

    keeper = LeaseKeeper(broker, job_id, token, lease_seconds)
    keeper.start()

    workdir = Path(tempfile.mkdtemp(prefix=f"sgfjob-{job_id[:8]}-"))
    try:
        sgf = workdir / (claimed.get("sgf_name") or "game.sgf")
        if not sgf.suffix:
            sgf = sgf.with_suffix(".sgf")
        sgf.write_text(claimed["sgf_text"], encoding="utf-8")
        outdir = workdir / "out"

        broker.progress(job_id, token, 5, "worker started")
        if fake:
            code, detail = run_fake_pipeline(outdir)
        else:
            if toolkit is None:
                raise RuntimeError("no --toolkit given and not --fake-pipeline")
            code, detail = run_real_pipeline(toolkit, sgf, outdir, claimed, extra)

        if keeper.lost:
            raise LeaseLost(409, f"lease lost while running: {keeper.lost}")

        if code != 0:
            log(f"  pipeline failed (exit {code})")
            broker.complete(job_id, token, success=False,
                            error=f"pipeline exit {code}: {detail}")
            return False

        broker.progress(job_id, token, 85, "pipeline finished, uploading")

        arts = collect_artifacts(outdir)
        if not arts:
            raise RuntimeError(f"pipeline produced no uploadable artifacts in {outdir}")
        for kind, path in arts:
            data = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            broker.upload(job_id, token, kind, path.name, data, mime)
            log(f"  uploaded {kind:<15} {path.name:<28} {len(data)/1e6:7.2f} MB")

        broker.complete(job_id, token, success=True)
        log(f"job {job_id} SUCCEEDED ({len(arts)} artifacts)")
        return True

    except LeaseLost as e:
        # another worker owns this job now; its result is the one that counts
        log(f"job {job_id} ABANDONED: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        log(f"job {job_id} FAILED: {type(e).__name__}: {e}")
        try:
            broker.complete(job_id, token, success=False,
                            error=f"{type(e).__name__}: {e}")
        except Exception as e2:  # noqa: BLE001
            log(f"  could not report failure: {e2}")
        return False
    finally:
        STOP.set()
        keeper.join(timeout=2)
        STOP.clear()
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", default=os.environ.get(
        "BROKER_URL", "http://127.0.0.1:8000/api/v1"))
    ap.add_argument("--key", default=os.environ.get("WORKER_KEY"))
    ap.add_argument("--name", default=os.environ.get("WORKER_NAME", "worker-1"))
    ap.add_argument("--toolkit", type=Path,
                    default=Path(os.environ.get(
                        "TOOLKIT_DIR",
                        "/home/wei/playground/goGames/selfCommentary")))
    ap.add_argument("--lease-seconds", type=int, default=3600)
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true",
                    help="process one job then exit (0 if one was processed)")
    ap.add_argument("--max-jobs", type=int, default=0, help="0 = unlimited")
    ap.add_argument("--fake-pipeline", action="store_true",
                    help="skip the real render; exercise the queue plumbing")
    ap.add_argument("--extra-arg", action="append", default=[],
                    help="extra flag passed through to go_music_video.py")
    args = ap.parse_args()

    if not args.key:
        print("error: no worker key; pass --key or set WORKER_KEY", file=sys.stderr)
        return 2
    if not args.fake_pipeline and not (args.toolkit / "go_music_video.py").is_file():
        print(f"error: no go_music_video.py in {args.toolkit}", file=sys.stderr)
        return 2

    broker = Broker(args.broker, args.key)
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())

    try:
        broker.heartbeat({"toolkit": str(args.toolkit),
                          "fake": args.fake_pipeline})
    except BrokerError as e:
        print(f"error: cannot reach broker at {args.broker}: {e}", file=sys.stderr)
        return 2

    log(f"worker {args.name!r} -> {args.broker}"
        f"{' (FAKE PIPELINE)' if args.fake_pipeline else ''}")

    processed = 0
    idle = 0
    while not STOP.is_set():
        try:
            claimed = broker.claim(args.name, args.lease_seconds)
        except BrokerError as e:
            log(f"claim failed: {e}")
            if STOP.wait(args.poll_interval * 2):
                break
            continue

        if not claimed:
            idle += 1
            if args.once:
                log("queue empty")
                return 0
            if idle % 12 == 1:
                log("queue empty, waiting")
            if STOP.wait(args.poll_interval):
                break
            continue

        idle = 0
        process_one(broker, args.toolkit, claimed, args.lease_seconds,
                    args.fake_pipeline, list(args.extra_arg))
        processed += 1
        try:
            broker.heartbeat({"processed": processed})
        except BrokerError:
            pass
        if args.once or (args.max_jobs and processed >= args.max_jobs):
            break

    log(f"stopping after {processed} job(s)")
    return 0 if processed or args.once else 0


if __name__ == "__main__":
    sys.exit(main())
