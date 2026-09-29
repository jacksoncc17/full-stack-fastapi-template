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

from worker_plugins.base import PluginError
from worker_plugins.loader import describe, load_plugins

STOP = threading.Event()

# The worker is deliberately service-agnostic: it stages input, dispatches to a
# plugin, uploads whatever the plugin returns, and reports. Everything service
# specific -- which script to run, which flags, which file is which artifact kind --
# lives in worker_plugins/. That is what makes a new service addable without
# touching this file.


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class BrokerError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class BrokerUnreachable(BrokerError):
    """The broker could not be reached at all.

    Distinct from an HTTP error: urllib raises raw OSError subclasses
    (ConnectionResetError, ConnectionRefusedError, timeout) rather than HTTPError,
    so without translating them the main loop's `except BrokerError` misses them
    and the worker dies on the first network blip -- for example when the API is
    restarted, or a load balancer drops an idle connection. A worker that exits on
    a transient error is worse than useless: it silently stops taking work and the
    queue looks stalled for no visible reason.

    status 0 is used because there was no HTTP response.
    """

    def __init__(self, detail: str):
        super().__init__(0, detail)


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
        except (urllib.error.URLError, OSError, ValueError) as e:
            # no HTTP response at all: DNS, refused, reset, timeout, or a body
            # that was not JSON (e.g. a proxy error page)
            raise BrokerUnreachable(f"{type(e).__name__}: {e}") from e

    def input_meta(self, job_id: str, name: str) -> dict:
        for row in self._request("GET", f"/worker/jobs/{job_id}/inputs"):
            if row["name"] == name:
                return row
        raise BrokerError(404, f"input {name!r} not listed for job {job_id}")

    def input_data(self, job_id: str, name: str) -> bytes:
        url = f"{self.base}/worker/jobs/{job_id}/inputs/{name}/data"
        req = urllib.request.Request(url, headers={"X-Worker-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise BrokerError(e.code, e.read().decode("utf-8", "replace")[:200])
        except (urllib.error.URLError, OSError) as e:
            raise BrokerUnreachable(f"{type(e).__name__}: {e}") from e

    def claim(self, worker_name: str, lease_seconds: int):
        return self._request(
            "POST", "/worker/claim",
            {"worker_name": worker_name, "lease_seconds": lease_seconds},
        )

    def heartbeat(self, capabilities=None):
        return self._request("POST", "/worker/heartbeat",
                             {"capabilities": capabilities or {}})

    def publish_service(self, *, name, title, description=None,
                        param_schema=None, artifact_kinds=None, version="1"):
        """Register this worker's service contract and bind the worker to it.

        This is the whole registration step: the broker stores the schema for
        validation and for the frontend form, and binds the worker so the claim
        query routes only this service's jobs here.
        """
        return self._request("POST", "/worker/services", {
            "name": name, "title": title, "description": description,
            "param_schema": param_schema, "artifact_kinds": list(artifact_kinds or []),
            "version": version,
        })

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


def _first_line(text: str | None) -> str | None:
    """First non-empty line of a docstring, for the service description."""
    if not text:
        return None
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()[:200]
    return None


def stage_input(broker: Broker, claimed: dict, workdir: Path) -> dict[str, list[Path]]:
    """Put the job's input files into the working directory.

    Two sources, because they exist for different reasons:

    * uploaded inputs, fetched from the broker by name -- the general path, since a
      service may need several named files and their bytes are far too large to inline
      in a job row
    * `payload`, the inline text a legacy SGF submission carries

    Filenames are preserved: the SGF pipeline derives its output names from the input
    stem, so renaming to something generic would change what the user receives.
    """
    staged: dict[str, list[Path]] = {}

    for name in (claimed.get("inputs") or []):
        meta = broker.input_meta(claimed["job_id"], name)
        filename = Path(meta.get("filename") or f"{name}.bin").name
        dest = workdir / filename
        dest.write_bytes(broker.input_data(claimed["job_id"], name))
        staged.setdefault(name, []).append(dest)
        log(f"  staged input {name!r} as {filename} "
            f"({dest.stat().st_size/1e6:.2f} MB)")

    if not staged:
        name = "payload"
        path = workdir / Path(claimed.get("sgf_name") or "input.txt").name
        path.write_text(claimed.get("payload") or "", encoding="utf-8")
        staged[name] = [path]

    return staged


def process_one(broker: Broker, plugin, claimed: dict, lease_seconds: int,
                fake: bool, extra: list[str]) -> bool:
    """Run one job through its service plugin.

    All service knowledge is behind `plugin`: this function only stages input,
    dispatches, uploads what came back, and reports.
    """
    job_id = claimed["job_id"]
    token = claimed["lease_token"]
    log(f"claimed job {job_id} (attempt {claimed['attempts']}) "
        f"service={claimed.get('service')}")

    keeper = LeaseKeeper(broker, job_id, token, lease_seconds)
    keeper.start()

    workdir = Path(tempfile.mkdtemp(prefix=f"job-{job_id[:8]}-"))
    outdir = workdir / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    try:
        staged = stage_input(broker, claimed, workdir)
        broker.progress(job_id, token, 5, "worker started")

        def progress(pct: int, message: str) -> None:
            # a plugin reporting progress is also a liveness signal, so a long
            # render keeps its lease alive through this path
            try:
                broker.progress(job_id, token, max(5, min(95, pct)), message)
            except LeaseLost:
                raise
            except BrokerError as e:
                log(f"  progress update failed (continuing): {e}")

        if fake:
            # each service owns its own stub, so the worker stays service-agnostic
            fake_fn = getattr(plugin, "fake_run", None)
            if fake_fn is None:
                raise RuntimeError(
                    f"service {plugin.name!r} has no fake_run(); --fake-pipeline "
                    f"cannot be used with it"
                )
            produced = fake_fn(claimed.get("params") or {}, workdir, outdir,
                               progress, staged)
        else:
            params = dict(claimed.get("params") or {})
            if extra:
                # pass-through flags only make sense for the pipeline; keep them
                # available to plugins that want them
                params.setdefault("_extra_args", extra)
            produced = plugin.run(params, workdir, outdir, progress, staged)

        if keeper.lost:
            raise LeaseLost(409, f"lease lost while running: {keeper.lost}")

        if not produced:
            raise RuntimeError(f"service {plugin.name!r} produced no artifacts")

        broker.progress(job_id, token, 90, "uploading artifacts")
        for art in produced:
            path = Path(art["path"])
            data = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            broker.upload(job_id, token, art["kind"], path.name, data, mime)
            log(f"  uploaded {art['kind']:<16} {path.name:<30} {len(data)/1e6:7.2f} MB")

        broker.complete(job_id, token, success=True)
        log(f"job {job_id} SUCCEEDED ({len(produced)} artifacts)")
        return True

    except LeaseLost as e:
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
                        "/home/wei/playground/goGames/selfCommentary")),
                    help="passed to plugins that need it (the SGF pipeline does)")
    ap.add_argument("--service", default=None,
                    help="run only this service. Default: run every plugin found, "
                         "which is fine because a worker is bound to one service by "
                         "the broker -- use this to restrict what is loaded")
    ap.add_argument("--plugin-dir", action="append", default=[],
                    help="extra directory of service plugins (repeatable)")
    ap.add_argument("--list-services", action="store_true",
                    help="show the plugins this worker would load, then exit")
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

    # ---- load service plugins -------------------------------------------
    # Fatal on error: a worker that starts with a broken plugin and then fails every
    # job it claims is worse than one that refuses to start.
    try:
        plugins = load_plugins(
            [Path(d) for d in args.plugin_dir] or None,
            toolkit=args.toolkit, extra_args=list(args.extra_arg),
        )
    except PluginError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.service:
        if args.service not in plugins:
            print(f"error: --service {args.service!r} is not among the loaded "
                  f"plugins: {sorted(plugins)}", file=sys.stderr)
            return 2
        plugins = {args.service: plugins[args.service]}

    if args.list_services:
        print(f"services this worker can run ({len(plugins)}):")
        print(describe(plugins))
        return 0

    if not args.key:
        print("error: no worker key; pass --key or set WORKER_KEY", file=sys.stderr)
        return 2

    broker = Broker(args.broker, args.key)
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())

    try:
        broker.heartbeat({"toolkit": str(args.toolkit),
                          "fake": args.fake_pipeline})
    except BrokerError as e:
        # A single failed heartbeat at startup is not proof the broker is gone
        # (a restarting API is the common case), so warn and let the claim loop
        # keep retrying rather than refusing to start.
        print(f"warning: broker not reachable yet at {args.broker}: {e}; "
              f"will keep retrying", file=sys.stderr)

    log(f"worker {args.name!r} -> {args.broker}"
        f"{' (FAKE PIPELINE)' if args.fake_pipeline else ''}")

    # Publish each service's contract and bind this worker to it. This is the whole
    # registration step: the broker learns the param schema (for validation and the
    # frontend form) and which service to route here.
    if len(plugins) > 1 and not args.service:
        log(f"loaded {len(plugins)} services; publishing all of them. The broker "
            f"binds a worker to ONE service (the last published wins) -- use "
            f"--service to be explicit when several are loaded.")
    for name, plugin in plugins.items():
        try:
            res = broker.publish_service(
                name=plugin.name, title=plugin.title,
                description=_first_line(plugin.__doc__),
                param_schema=plugin.param_schema,
                artifact_kinds=plugin.artifact_kinds,
            )
            log(f"  published service {res.get('service')!r} v{res.get('version')}")
        except BrokerError as e:
            print(f"error: could not publish service {name!r}: {e}", file=sys.stderr)
            return 2

    processed = 0
    idle = 0
    while not STOP.is_set():
        try:
            claimed = broker.claim(args.name, args.lease_seconds)
        except BrokerError as e:
            # a broker outage is routine, not fatal: back off and keep trying
            unreachable = isinstance(e, BrokerUnreachable)
            wait = min(60.0, args.poll_interval * (4 if unreachable else 2))
            log(f"claim failed ({'unreachable' if unreachable else 'http'}): {e}"
                f" -- retrying in {wait:.0f}s")
            if STOP.wait(wait):
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
        service = claimed.get("service") or "sgf_video"
        plugin = plugins.get(service)
        if plugin is None:
            # The broker routed us a job for a service we do not have. That means the
            # two disagree about what this worker runs, so fail the job loudly rather
            # than reporting a misleading success.
            log(f"claimed job for unknown service {service!r}; failing it "
                f"(this worker runs {sorted(plugins)})")
            try:
                broker.complete(claimed["job_id"], claimed["lease_token"],
                                success=False,
                                error=f"worker does not implement service {service!r}")
            except BrokerError as e:
                log(f"  could not report it: {e}")
            continue
        process_one(broker, plugin, claimed, args.lease_seconds,
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
