# Deploying to DigitalOcean

One droplet runs the broker (API + frontend + Postgres). Your **worker stays on
your LAN**, because it has to reach KataGo over SSH and ACE-Step over HTTP — see
"Architecture" below. That split is the whole point of the design, and it is what
makes a cheap droplet sufficient.

## Architecture

```
        INTERNET                         YOUR LAN
  ┌──────────────────────┐        ┌──────────────────────────┐
  │ DigitalOcean droplet │        │ worker.py                │
  │  Traefik (443, LE)   │◀───────│   polls the broker       │
  │  backend  :8000      │  HTTPS │   runs go_music_video.py │
  │   - /api/v1/*        │        │   uploads artifacts      │
  │   - the built SPA    │        └──────────────────────────┘
  │  db (postgres)       │             needs: KataGo host,
  └──────────────────────┘                     ACE-Step server
     holds jobs + results
```

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

Tables and the first superuser are created by `backend/scripts/prestart.sh`, which
the backend entrypoint runs — no manual migration step.

### 7. Register a worker, then run it on your LAN

```bash
# on the droplet, or from anywhere with an admin login
curl -s -X POST https://<domain>/api/v1/admin/workers/ \
  -H "Authorization: Bearer <admin token>" \
  -H 'Content-Type: application/json' \
  -d '{"name":"lan-worker-1"}'
# => {"api_key": "...", ...}   copy it, it is shown once
```

Then on the LAN machine that can reach KataGo and ACE-Step:

```bash
export WORKER_KEY=<api_key>
python3 worker.py --broker https://<domain>/api/v1 \
  --name lan-worker-1 --toolkit /path/to/selfCommentary
```

Run it under `systemd` or `tmux` so it survives logout — see `worker.service`.

## Operational notes

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
