#!/usr/bin/env bash
# Restart the dev API container.
#
# The API is a plain docker container (not docker compose), so "restart" means
# recreate it. This script exists because the full docker run line is long and easy
# to get subtly wrong -- for example forgetting -v "$PWD":/w, which makes the
# container run stale code, or dropping an env var, which silently changes
# behaviour (FRONTEND_HOST drives CORS; .env / .env.secret are read from the
# mounted repo, so GOOGLE_* must NOT be passed here or they would override them).
#
#   ./dev-restart-api.sh          # recreate on port 8000
#   ./dev-restart-api.sh 8001     # or a different host port
#
# Google credentials are read from .env / .env.secret inside the mount, so after
# editing them you only need to run this.
set -euo pipefail
cd "$(dirname "$0")"

HOST_PORT="${1:-8000}"
NAME=dsh-api
PG=dsh-pg
NET=dsh-net

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }

# the database container must exist: nothing persists without it
if ! docker inspect "$PG" >/dev/null 2>&1; then
  echo "==> creating postgres container ($PG)"
  docker network create "$NET" >/dev/null 2>&1 || true
  docker run -d --name "$PG" --network "$NET" \
    -e POSTGRES_PASSWORD=dshpass -e POSTGRES_DB=app \
    -p 127.0.0.1:55432:5432 postgres:18 >/dev/null
  for _ in $(seq 1 30); do
    docker exec "$PG" pg_isready -U postgres -d app >/dev/null 2>&1 && break
    sleep 1
  done
fi

echo "==> recreating $NAME on port $HOST_PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network "$NET" -p "127.0.0.1:${HOST_PORT}:8000" \
  -v "$PWD":/w -w /w/backend \
  -e DATABASE_URL='postgresql+psycopg://postgres:dshpass@dsh-pg:5432/app' \
  -e SECRET_KEY=devsecret \
  -e FIRST_SUPERUSER=admin@example.com \
  -e FIRST_SUPERUSER_PASSWORD=changethis \
  -e PROJECT_NAME=GoVid \
  -e SMTP_HOST=localhost -e EMAILS_FROM_EMAIL=a@b.c \
  -e FASTAPI_ENV=development \
  -e FRONTEND_HOST=http://localhost:5173 \
  python:3.14-slim sh -c '
    pip install -q sqlmodel fastapi uvicorn "psycopg[binary]" pydantic-settings \
      email-validator python-multipart jinja2 emails "pwdlib[argon2,bcrypt]" \
      PyJWT "sentry-sdk[fastapi]" httpx >/dev/null 2>&1
    exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
  ' >/dev/null

echo "==> waiting for the API (first start installs deps, ~60s)"
for i in $(seq 1 60); do
  if curl -fsS -m 3 "http://127.0.0.1:${HOST_PORT}/api/v1/utils/health-check/" >/dev/null 2>&1; then
    echo "==> up on http://localhost:${HOST_PORT}"
    curl -s -m 5 "http://127.0.0.1:${HOST_PORT}/api/v1/auth/google/status" \
      | sed 's/^/    google auth: /'
    echo
    exit 0
  fi
  sleep 2
done

echo "!! API did not come up; last 30 log lines:" >&2
docker logs "$NAME" 2>&1 | tail -30 >&2
exit 1
