# SGF video job queue

Adds an asynchronous job queue to this template: users submit SGF render
requests, workers claim and process them, results come back to the API.

## Architecture

```
  browser ──submit──▶  API (FastAPI)  ──┐
                          ▲             │ job row, status=queued
                          │             ▼
                    artifacts      ┌─────────┐
                          │        │ Postgres│  the queue IS this table
                          └────────┤  job    │
                                   └─────────┘
                                        ▲
                     claim / progress / │ artifacts / complete
                                        │
                                   worker (X-Worker-Key)
                                        │
                                        ▼
                              go_music_video.py pipeline
```

There is no Redis/RabbitMQ in this stack, so Postgres is the queue. Claiming uses
`SELECT ... FOR UPDATE SKIP LOCKED`, which lets N workers poll concurrently
without blocking or double-claiming.

## Files added

| file | purpose |
|---|---|
| `backend/app/models_jobs.py` | `job`, `artifact`, `artifactblob`, `jobevent`, `worker` |
| `backend/app/crud_jobs.py` | claiming, leasing, progress, artifact upsert, completion |
| `backend/app/api/routes/jobs.py` | user routes: submit, list, summary, events, download, cancel |
| `backend/app/api/routes/worker.py` | worker routes: claim, progress, renew, upload, complete |
| `backend/_verify_queue.py` | 19 checks against a real Postgres |
| `backend/_dev_bootstrap.py` | creates a dev user + worker key |

`backend/app/api/main.py` registers the two new routers.

## Design decisions worth knowing

**Artifacts are in Postgres, in a separate table.** `Artifact` is metadata only;
`ArtifactBlob` holds the bytes. The dashboard polls artifact *lists*, so keeping
the bytes out of that table means those queries never load them. Postgres TOASTs
and compresses large values, so a ~10 MB mp4 works. `Artifact.storage_backend` is
`"db"` today and the shape is storage-agnostic, so moving to S3 later means adding
a backend rather than migrating the schema.

**Leases, not locks.** A claimed job carries `lease_expires_at`. If a worker dies,
`reap_expired_leases` returns the job to the queue (or fails it once `attempts`
reaches `max_attempts`). Reaping runs opportunistically on each claim, so there is
no separate sweeper to deploy.

**Every worker write is guarded by `lease_token`.** Not belt-and-braces: without
it, a worker that stalls and wakes up after its lease lapsed will overwrite the
result of the worker that took over. The guard turns that into a clean 409.

**Workers are not users.** Workers authenticate with `X-Worker-Key` (stored
hashed); user JWTs cannot claim jobs, and a worker cannot read another tenant's
job. Cancelling a running job cannot interrupt the worker — there is no channel —
so it marks the job canceled and the lease guard discards the worker's final write.

## Verified

Schema and queue semantics, against real Postgres 18 on Python 3.14:

```
[PASS] all 5 queue tables created
[PASS] no job claimed twice            10 claims, 10 unique   <- 2 threads, 10 jobs
[PASS] all 10 queued jobs claimed      claimed 10/10
[PASS] expired lease requeued
[PASS] second worker gets the requeued job
[PASS] stale lease write rejected
[PASS] exhausted job fails instead of requeueing
[PASS] artifact stored / re-upload upserts, does not duplicate
[PASS] blob round-trips / metadata list does not need blobs
[PASS] write to finished job rejected
[PASS] cancel sets terminal state
19/19 checks passed
```

And the worker HTTP flow with curl against a running API:

| step | result |
|---|---|
| `POST /worker/claim` | 200, returns sgf_text + params + lease_token |
| `POST /worker/jobs/{id}/progress` | 200 |
| `POST /worker/jobs/{id}/artifacts` (multipart) | 200, sha256 recorded |
| stale lease token | **409** |
| bad worker key | **403** |
| `POST /worker/jobs/{id}/complete` | 200, status=succeeded |

## Run it locally

```bash
docker network create dsh-net
docker run -d --name dsh-pg --network dsh-net -e POSTGRES_PASSWORD=dshpass \
  -e POSTGRES_DB=app -p 127.0.0.1:55432:5432 postgres:18

# schema + tests (Python 3.14: the template requires >=3.14, and its models.py
# relies on PEP 649 deferred annotations, so 3.13 cannot even import it)
docker run --rm --network dsh-net -v $PWD:/w -w /w/backend \
  -e DATABASE_URL=postgresql+psycopg://postgres:dshpass@dsh-pg:5432/app \
  python:3.14-slim sh -c 'pip install -q sqlmodel "psycopg[binary]" && python _verify_queue.py'
```

## Still to do

1. **Alembic migration.** The models work and `create_all` builds them, but the
   template ships Alembic and there is no revision yet, so a deployed database
   would not get the tables. This is the next thing to do.
2. **The worker process itself** — a loop that claims, runs `go_music_video.py`
   in a temp dir, uploads outputs, and reports progress. The pipeline half-exists;
   it needs the queue-facing half.
3. **Google OAuth** (`oauth_account` table + `/auth/google` callback) alongside
   the existing email/password login.
4. **Frontend**: a submit page (SGF upload + parameters) and a dashboard (job
   table, status counts, progress timeline, artifact downloads), plus regenerating
   the typed client from `openapi.json`.
5. **Worker registration endpoint** (admin-only) so keys are not created by script.
6. **The pipeline reaches off-box.** `go_music_video.py` shells out to
   `remote_katago.py` (SSH to the KataGo host) and `ace_client.py` (the ACE-Step
   server). A worker must run where those are reachable, or they need exposing.

## Alembic migration

`backend/app/alembic/versions/a66c3a2d72c4_add_job_queue_tables.py` chains from the
template's head (`fe56fa70289e`) and creates the five queue tables.

**It is hand-ordered, not autogenerated output.** Autogenerate sorted the
statements by table name and the first run failed with
`relation "job" does not exist` while creating `artifact`, because `job` and
`artifact` reference each other:

```
job.reference_audio_id -> artifact.id    (optional reference track)
artifact.job_id        -> job.id
```

No ordering of `CREATE TABLE` satisfies both, so `job` is created without that one
constraint and it is added afterwards with `ALTER TABLE ... ADD CONSTRAINT`. The
downgrade breaks the cycle first for the same reason.

`env.py` also needed the job models imported explicitly -- `target_metadata` is
`SQLModel.metadata`, and a table only enters it once its module has been imported,
so without that autogenerate emits an empty migration.

Verified against a fresh database on Python 3.14:

| operation | result |
|---|---|
| `upgrade head` from empty | 6 migrations, all tables incl. the 5 new ones |
| `downgrade -1` | exactly the 5 queue tables dropped, template tables untouched |
| `upgrade head` again | 5 tables restored |

## Worker loop (`worker.py`)

Claims jobs, runs the render, uploads artifacts, reports completion.

```bash
export WORKER_KEY=...
python3 worker.py --broker http://127.0.0.1:8000/api/v1 \
    --toolkit /path/to/selfCommentary --name worker-1
```

`--once` processes one job and exits. `--fake-pipeline` skips the real render and
fabricates artifacts, so the queue plumbing can be exercised without spending
minutes per job.

**Standard library only.** The toolkit needs Pillow and ffmpeg and runs on the
system interpreter; the broker needs SQLModel on Python 3.14. The worker is
deliberately dependency-free so a change to either environment cannot break it.

**The lease is renewed on a background thread**, and losing it is fatal: it means
another worker owns the job, so the run stops rather than doing work whose upload
will be rejected. `--lease-seconds` defaults to an hour because a real render is
minutes long and the lease is the only thing preventing a duplicate claim.

**The SGF keeps its original filename.** `go_music_video.py` derives its output
directory and filenames from the SGF stem, so writing it as `game.sgf` would lose
the naming the user expects.

**Artifacts upload one file at a time**, so a job that fails halfway still leaves
its successful outputs visible, and `store_artifact` upserts by `(job, kind)` so a
retry replaces rather than duplicates.

### Verified

Against the live API, with the fake pipeline:

```
[21:33:05] claimed job 80ccbba9-...(attempt 1) dur=60.0s formats=mp4,webp
[21:33:05]   uploaded mp3             fake.mp3      0.00 MB
[21:33:05]   uploaded mp4             fake.mp4      0.00 MB
[21:33:05]   uploaded webp            fake.webp     0.00 MB
[21:33:05]   uploaded programme_html  programme.html 0.00 MB
[21:33:05] job 80ccbba9-... SUCCEEDED (4 artifacts)
```

Database after the run: job `succeeded`, progress 100, attempts 1, and each
artifact's `claimed` size equals its stored `bytea` length with the mime type
inferred per kind. The event log captured the whole path including an earlier
lease-expiry → requeue → retry sequence from the concurrency test.

### A bug this found

`Worker.capabilities` was declared `dict` on a `Text` column. That works until a
worker actually reports capabilities, at which point psycopg raises
`cannot adapt type 'dict'` and the heartbeat returns 500. It is now a `JSON`
column. The queue verification suite was re-run afterwards (19/19) to confirm the
change did not regress anything.
