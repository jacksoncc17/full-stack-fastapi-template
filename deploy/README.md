# Deploying to DigitalOcean

One droplet runs the broker (API + frontend + Postgres). Your **worker stays on
your LAN**, because it has to reach KataGo over SSH and ACE-Step over HTTP — see
"Architecture" below. That split is the whole point of the design, and it is what
makes a cheap droplet sufficient.

## Architecture

```
        INTERNET                         YOUR LAN
  ┌──────────────────────┐        ┌──────────────────────────┐
  │ DigitalOcean droplet │        │ worker (one per service) │
  │  Traefik (443, LE)   │◀───────│  lan-sgf                 │
  │  backend  :8000      │  HTTPS │   runs go_music_video.py │
  │   - /api/v1/*        │        │  lan-intonation          │
  │   - the built SPA    │        │   aligns clips to score  │
  │  db (postgres)       │        └──────────────────────────┘
  └──────────────────────┘           lan-sgf needs: KataGo
     holds jobs + results                     host, ACE-Step
                                     lan-intonation needs:
                                       neither -- just its venv
```

Nothing on the LAN needs a public IP, a port forward or dynamic DNS. The broker is idle
between jobs, which is what makes a small droplet sufficient -- it holds the queue and
the artifacts, and never renders.

The worker only ever makes **outbound HTTPS** calls, so nothing on your home
network needs a public IP, port forward, or dynamic DNS.

## Steps

### 1. Create the droplet

Any Ubuntu 24.04 droplet with **2 GB RAM / 1 vCPU** is enough for the broker (it
does not render). 25 GB disk is fine to start. Add your SSH key.

You can instead paste `cloud-init.yaml` into the droplet's **User data** field and
skip steps 2–3.

### 2. Point a domain at it

Create an `A` record to the droplet IP, e.g. `broker.example.com`. HTTPS is
required — the worker sends an API key, and browser login sends a bearer token.

### 3. Install Docker

```bash
ssh root@<droplet-ip>
curl -fsSL https://get.docker.com | sh
```

### 4. Get the code onto the droplet

```bash
git clone <your repo> app && cd app/full-stack-fastapi-template
```

### 5. Create `.env` with real secrets

> **The `FASTAPI_ENV=development` line is the trap.** The repo's committed `.env`
> contains it, and it is what *disables* the placeholder-secret check. If you copy
> that file to a droplet unchanged, production silently runs with `changethis`
> secrets and a `SECRET_KEY` that anyone can read in git — every JWT becomes
> forgeable. `deploy/env.production.example` deliberately leaves it commented out.
>
> Verified: with `FASTAPI_ENV` unset the app refuses to start —
> `ValueError: The value of SECRET_KEY is "changethis"` — and with it set to
> `development` the same config loads with only a warning. So the check works, but
> only if this line is absent on the server.


**The app refuses to start if any secret is left as `changethis`** (outside
development), so these must be generated:

```bash
cp deploy/env.production.example .env      # replaces the dev .env, do not merge
python3 -c "import secrets;print('SECRET_KEY='+secrets.token_urlsafe(32))"
python3 -c "import secrets;print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24))"
python3 -c "import secrets;print('FIRST_SUPERUSER_PASSWORD='+secrets.token_urlsafe(16))"
# paste those in, set DOMAIN, and set GOOGLE_* for your domain

# confirm the safety check is armed before starting:
grep -c '^FASTAPI_ENV' .env     # expect 0
```

`.env.secret` is a *local development* convenience (it exists because the dev
`.env` is committed). In production `.env` itself is not committed, so you can put
the Google secret straight in it and ignore `.env.secret` entirely.

### 6. Start it

```bash
./deploy/deploy.sh
```

`deploy.sh` does the steps above and refuses to proceed on the misconfigurations
that fail silently in production. It:

* creates `.env` from the production template if absent
* generates `SECRET_KEY`, `POSTGRES_PASSWORD` and `FIRST_SUPERUSER_PASSWORD` when
  they are missing or still `changethis` — and **only** then, so re-running never
  rotates the database password out from under an initialised volume
* resolves `${DOMAIN}`-style references inside `.env`. Docker Compose interpolates
  those in the compose files but **not** in `.env` values, so without this
  `FRONTEND_HOST` reaches the app as the literal string `${DOMAIN}`, which breaks
  CORS and the OAuth redirect silently
* fails on: a missing/placeholder `DOMAIN`, any `FASTAPI_ENV=development` line,
  placeholder secrets, and a `DATABASE_URL` whose host is not `db`
* warns on a non-HTTPS `FRONTEND_HOST` and a `GOOGLE_REDIRECT_URI` that does not
  mention `$DOMAIN`
* pulls, builds, starts with `--wait`, then prints the worker registration command

```bash
./deploy/deploy.sh --dry-run     # report problems, change nothing
./deploy/deploy.sh --no-build    # later deploys, reuse the images
./deploy/deploy.sh --no-pull     # skip git pull
```

Equivalent by hand:

```bash
docker compose -f compose.yml -f compose.deploy.yml up -d --build
```

First build takes several minutes (it compiles the frontend and installs the
Python deps). Then:

```bash
docker compose logs -f backend      # watch it come up
curl -s https://<domain>/api/v1/utils/health-check/    # -> true
```

Tables and the first superuser are created by `backend/scripts/prestart.sh` — `alembic
upgrade head` then `initial_data.py` — which the image's **entrypoint** runs before
starting the server. No manual migration step, and nothing to remember.

That sentence was false until recently: the image declared only
`CMD ["fastapi", "run", ...]` with no entrypoint, and nothing in the deploy path invoked
`prestart.sh`. A fresh droplet then came up reporting healthy with an empty database
schema, so the first login failed with a bare 400 and any query died on

```
sqlalchemy.exc.ProgrammingError: (psycopg.errors.UndefinedTable) relation "user" does not exist
```

**If you deployed before this fix**, the database on that droplet has no tables. Either
redeploy (the entrypoint will create everything on start) or run it once by hand:

```bash
docker compose -f compose.yml -f compose.deploy.yml run --rm --entrypoint bash \
  backend -c 'bash scripts/prestart.sh'
```

### 7. Register workers, then run them on your LAN

**One worker runs one service.** The broker keeps a single `WorkerService` row per
worker and the claim query filters `Job.service == service`, so a worker that announces
two services ends up bound to whichever was published last. Run one worker per service,
each with its own API key.

```bash
# once per service -- the key is shown only here
for name in lan-sgf lan-intonation; do
  curl -s -X POST https://<domain>/api/v1/admin/workers/ \
    -H "Authorization: Bearer <admin token>" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\"}"
done
```

Then on the LAN machine that can reach KataGo and ACE-Step:

```bash
# sgf_video -- needs the toolkit (go_music_video.py), plus KataGo and ACE-Step
.venv/bin/python worker.py --broker https://<domain>/api/v1 --key <key-sgf> \
  --name lan-sgf --service sgf_video --toolkit /path/to/selfCommentary

# intonationCheck -- needs the service venv; NO toolkit, KataGo or ACE-Step
.venv-intonation/bin/python worker.py --broker https://<domain>/api/v1 \
  --key <key-intonation> --name lan-intonation --service intonationCheck \
  --plugin-dir /path/to/selfCommentary/services
```

Two details that are easy to get wrong:

* **`--service` is not optional once a worker can see several plugins.** Without it the
  worker publishes every plugin it loaded and warns that the last one wins, which leaves
  it silently claiming only that service and quietly ignoring the rest of the queue.
* **Each worker needs the interpreter that has its own dependencies.** `intonationCheck`
  imports librosa, verovio, cairosvg and pymupdf, so it must run under
  `.venv-intonation/bin/python`; a bare `python3 worker.py` fails with
  `ModuleNotFoundError: No module named 'librosa'`. `sgf_video` needs the toolkit venv
  instead. The two services have deliberately different dependency sets, which is why
  they are separate processes rather than one worker with `--service` switched at runtime.

Run each under `systemd` or `tmux` so it survives logout. `worker.service` is a template:
copy it per service, change `User`, the paths, `WORKER_KEY` and the `--service` value —
its `ExecStart` as committed is the older single-service form and will not bind correctly
on its own.

## Operational notes

* **Adminer is gated off in the deploy stack, and should stay that way.** The base
  `compose.yml` enables Traefik for `adminer` and `compose.deploy.yml` gives it
  `Host(`adminer.${DOMAIN}`)` with TLS via the Let's Encrypt resolver, and there is no
  basic-auth middleware anywhere in the repo — `grep -rn "basicauth\|middlewares"
  compose*.yml` returns nothing. Left alone that is an unauthenticated database admin UI
  on the public internet, in front of the job queue and every worker API key. It was
  harmless in development only because `compose.override.yml` publishes it on
  `localhost:8080` instead. The template's own `deployment-docker-compose.md` tells you
  to create that DNS record, so following the documentation is what would expose it.

  `compose.deploy.yml` therefore puts it behind a profile, so it is not started by a
  deploy and not started by `deploy.sh`:

  ```
  docker compose -f compose.yml -f compose.deploy.yml config --services
  # proxy, db, backend        <- no adminer

  docker compose ... --profile debug up -d    # when you actually want it
  ```

  Verified: with the deploy overlay the default set is `proxy, db, backend`; adding
  `--profile debug` brings `adminer` back; the dev stack (`compose.yml` +
  `compose.override.yml`) still has it, unchanged. Only `proxy` publishes any port
  (80 and 443) and `db` publishes none, so Postgres is not reachable from outside —
  but an unauthenticated Adminer would have been a separate door into the same data.

* **The worker is the only thing that needs the LAN.** Because dispatch is
  pull-based, the worker can sit behind NAT indefinitely — it just polls.
* **Artifact bytes go to the `app-artifacts` volume** (`ARTIFACT_DIR=/data/artifacts`
  in `compose.yml`), not the database. Roughly 15 MB per job, so this keeps the
  database and its backups small.
  * It must be a **named volume**, not the container's `/tmp`. `/tmp` is destroyed
    when the container is recreated, and it is not shared between the four uvicorn
    worker processes — so an upload written by one process would 404 from another.
  * `docker compose down -v` deletes it, along with every rendered video. The queue
    and its metadata live in Postgres and would survive.
  * Unset `ARTIFACT_DIR` to fall back to storing bytes in Postgres (`artifactblob`),
    which is the default so that nothing changes unless you opt in. The backend in
    use is recorded per artifact row, so switching either way does not break
    downloads of older results.
  * Moving on to Spaces/S3 later is one class in `artifact_store.py` — the
    `storage_backend` / `storage_key` columns and the single read path are already
    in place.
* **Droplet sizing**: the broker is idle most of the time. If you move rendering to
  the cloud later you need the LAN dependencies solved first, and then a much
  larger machine.
* **Backups**: enable DigitalOcean droplet backups, or `pg_dump` on a cron and ship
  it to Spaces. Losing the database loses the queue _and_ every worker API key.
