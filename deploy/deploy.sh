#!/usr/bin/env bash
#
# Deploy the broker to a server (DigitalOcean droplet or any Docker host).
#
# Run this ON THE SERVER, from anywhere inside the repo:
#
#   ./deploy/deploy.sh                 # first run: generates secrets, builds, starts
#   ./deploy/deploy.sh --no-build      # later runs: reuse the built images
#   ./deploy/deploy.sh --dry-run       # show what it would do, change nothing
#
# The worker is NOT deployed here -- it stays on the LAN because it needs KataGo
# over SSH and ACE-Step over HTTP. See deploy/README.md.
#
# What it does, in order:
#   1. checks prerequisites and finds the repo root
#   2. creates .env from the template if absent, and generates any missing secrets
#   3. validates the settings that silently break production
#   4. optionally pulls the latest code
#   5. builds and starts the compose stack, waiting for healthy
#   6. prints the worker registration command
#
# It is idempotent: secrets are only generated when absent, so re-running does not
# rotate them out from under a running database.
set -euo pipefail

BUILD=1
PULL=1
DRY_RUN=0

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) BUILD=0 ;;
    --no-pull)  PULL=0 ;;
    --dry-run)  DRY_RUN=1 ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
  shift
done

# ---------------------------------------------------------------- helpers
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s[!]%s %s\n' "$c_ylw" "$c_off" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }
note() { printf '%s    %s%s\n' "$c_dim" "$*" "$c_off"; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '%s    would run: %s%s\n' "$c_dim" "$*" "$c_off"
  else
    "$@"
  fi
}

gen() { python3 -c "import secrets;print(secrets.token_urlsafe(${1:-32}))" 2>/dev/null \
        || openssl rand -base64 "${1:-32}" | tr -d '/+=' | cut -c1-"${1:-32}"; }

# ---------------------------------------------------------------- 1. locate + prereqs
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f compose.yml ] || die "compose.yml not found in $ROOT -- run this from inside the repo"
[ -f compose.deploy.yml ] || die "compose.deploy.yml not found in $ROOT"

command -v docker >/dev/null || die "docker not installed. On Ubuntu: curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing (need 'docker compose', not 'docker-compose')"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you root / in the docker group?)"
# python3 is used only for secret generation and validation
command -v python3 >/dev/null || die "python3 is required for secret generation"

say "repo root: $ROOT"

# ---------------------------------------------------------------- 2. .env + secrets
ENV_FILE="$ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  say "no .env -- creating one from deploy/env.production.example"
  if [ "$DRY_RUN" = 1 ]; then
    note "would copy deploy/env.production.example -> .env"
  else
    cp deploy/env.production.example "$ENV_FILE"
  fi
else
  say ".env already exists -- leaving secrets alone"
fi

# Assign or replace a KEY=value line, creating it when absent.
set_env() {
  local key="$1" val="$2"
  [ "$DRY_RUN" = 1 ] && { note "would set $key"; return 0; }
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # use a temp file rather than sed -i: values contain / and + which break s///
    awk -v k="$key" -v v="$val" \
      'BEGIN{FS=OFS="="} $1==k && !done {print k"="v; done=1; next} {print}' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }

# A secret is "missing" if the key is absent, empty, or still the template
# placeholder. Generating only in those cases is what makes re-running safe:
# silently rotating POSTGRES_PASSWORD on every run would lock the backend out of
# an already-initialised database volume.
need_secret() {
  local v; v="$(get_env "$1")"
  [ -z "$v" ] || [ "$v" = "GENERATE_ME" ] || [ "$v" = "changethis" ]
}

say "checking secrets"
for key in SECRET_KEY POSTGRES_PASSWORD FIRST_SUPERUSER_PASSWORD; do
  if need_secret "$key"; then
    val="$(gen 32)"
    set_env "$key" "$val"
    printf '    generated %s\n' "$key"
  else
    printf '    %s already set\n' "$key"
  fi
done

# The template writes references like https://${DOMAIN}/... in FRONTEND_HOST,
# DATABASE_URL and the Google URIs. Docker Compose interpolates ${VAR} in the
# compose files, but NOT inside .env values, so left alone those reach the app as
# the literal string "${DOMAIN}" -- which fails silently as a wrong CORS origin and
# a wrong OAuth redirect. Resolve them here instead.
say "resolving \${VAR} references inside .env"
if [ "$DRY_RUN" != 1 ]; then
  python3 - "$ENV_FILE" <<'PYEOF'
import os, re, sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
vals = {}
for ln in lines:
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, _, v = ln.partition("=")
        vals[k.strip()] = v
resolved, changed = [], []
for ln in lines:
    if "=" in ln and not ln.lstrip().startswith("#"):
        k, _, v = ln.partition("=")
        new = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}",
                     lambda m: vals.get(m.group(1), m.group(0)), v)
        if new != v:
            changed.append(k.strip())
        vals[k.strip()] = new
        ln = f"{k}={new}"
    resolved.append(ln)
path.write_text("\n".join(resolved) + "\n")
print(f"    resolved in: {', '.join(changed) if changed else '(nothing to resolve)'}")
PYEOF
else
  note "would resolve \${DOMAIN} and friends in .env"
fi

# ---------------------------------------------------------------- 3. validate
say "validating settings"
# Validation runs in dry-run too -- it is the part most worth checking before
# touching a server. Only the fatal exits are softened, so a dry run against a
# half-configured file still reports every problem instead of stopping at the first.
VALIDATE_HARD=1
[ "$DRY_RUN" = 1 ] && VALIDATE_HARD=0
problem() { [ "$VALIDATE_HARD" = 1 ] && die "$@"; warn "$@"; }

if true; then
  DOMAIN_VAL="$(get_env DOMAIN)"
  if [ -z "$DOMAIN_VAL" ] || [ "$DOMAIN_VAL" = "broker.example.com" ]; then
    problem "set DOMAIN in .env to your real hostname (Traefik needs it for the TLS certificate)"
  fi

  # THE trap: this line is what disables the placeholder-secret check, so a
  # production server must not have it. The committed dev .env does.
  if grep -qE '^FASTAPI_ENV=' "$ENV_FILE"; then
    problem ".env sets FASTAPI_ENV ($(get_env FASTAPI_ENV)). That disables the
    placeholder-secret check and enables dev-only routes. Remove the line, or set
    it to something other than 'development'."
  fi

  for key in SECRET_KEY POSTGRES_PASSWORD FIRST_SUPERUSER_PASSWORD; do
    v="$(get_env "$key")"
    [ -n "$v" ] || problem "$key is empty in .env"
    [ "$v" != "changethis" ] || problem "$key is still 'changethis' -- production would run with a public secret"
  done

  # inside compose the database is the 'db' service, never localhost
  DBURL="$(get_env DATABASE_URL)"
  case "$DBURL" in
    *"@db:"*) : ;;
    "") warn "DATABASE_URL is empty; the compose default expects postgresql://postgres:\${POSTGRES_PASSWORD}@db:5432/app" ;;
    *) problem "DATABASE_URL points somewhere other than the 'db' service:
    $DBURL
    Inside compose the host must be 'db'. The dev value (localhost) makes the
    backend look for Postgres on itself." ;;
  esac

  # CORS: the backend only answers the origin in FRONTEND_HOST
  FH="$(get_env FRONTEND_HOST)"
  case "$FH" in
    https://*) : ;;
    *) warn "FRONTEND_HOST is '$FH'; it should be https://$DOMAIN_VAL or the browser will block every API call" ;;
  esac

  note "domain: $DOMAIN_VAL"
  note "frontend host: $FH"
  GOOGLE_ID="$(get_env GOOGLE_CLIENT_ID)"
  if [ -n "$GOOGLE_ID" ]; then
    GRI="$(get_env GOOGLE_REDIRECT_URI)"
    note "google redirect: $GRI"
    case "$GRI" in
      *"$DOMAIN_VAL"*) : ;;
      *) warn "GOOGLE_REDIRECT_URI does not mention $DOMAIN_VAL -- Google will reject it with redirect_uri_mismatch" ;;
    esac
  else
    note "google sign-in: disabled (no GOOGLE_CLIENT_ID)"
  fi
fi

# ---------------------------------------------------------------- 4. pull
if [ "$PULL" = 1 ] && [ -d "$ROOT/.git" ]; then
  say "pulling latest code"
  if [ "$DRY_RUN" = 1 ]; then
    note "would run: git -C $ROOT pull --ff-only"
  elif run git -C "$ROOT" pull --ff-only; then
    :
  else
    # Do NOT continue here. Continuing means building the image from whatever code
    # happens to be on disk, which is how a failed pull turns into "I deployed the fix
    # and it did not take effect" -- the exact confusion this script exists to avoid.
    # `--no-pull` remains for deliberately deploying the working tree as-is.
    warn "git pull --ff-only failed -- refusing to deploy stale code"
    branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    upstream="$(git -C "$ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    echo >&2
    echo "    branch:   $branch" >&2
    echo "    upstream: ${upstream:-<none configured>}" >&2
    if [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
      # Only mention this when a fast-forward is actually possible. A push to an
      # unrelated branch also reports "diverged", and blaming local changes there
      # would send you off deleting edits that were never the problem.
      if [ -n "$upstream" ] && git -C "$ROOT" merge-base --is-ancestor \
           "$upstream" "$branch" 2>/dev/null; then
        echo "    local changes are blocking the fast-forward:" >&2
        git -C "$ROOT" status --short >&2 | head -5
        echo "      -> commit, stash, or discard them, then re-run" >&2
      fi
    fi
    if [ -n "$upstream" ] && ! git -C "$ROOT" merge-base --is-ancestor \
         "$branch" "$upstream" 2>/dev/null; then
      echo "    '$branch' and '$upstream' have DIVERGED -- no merge or rebase can" >&2
      echo "    reconcile unrelated histories. This usually means the remote branch" >&2
      echo "    was force-pushed, for example to repoint a default branch." >&2
      echo "      -> to take the remote exactly (discards local changes on tracked files):" >&2
      echo "           git -C $ROOT fetch origin" >&2
      echo "           git -C $ROOT reset --hard $upstream" >&2
    fi
    echo >&2
    echo "    Or deploy the working tree as-is, without pulling:" >&2
    echo "      ./deploy/deploy.sh --no-pull" >&2
    die "pull failed"
  fi
else
  note "skipping git pull"
fi

# ---------------------------------------------------------------- 5. build + up
COMPOSE=(docker compose -f compose.yml -f compose.deploy.yml)

say "starting the stack$( [ "$BUILD" = 1 ] && echo ' (building images -- first run takes several minutes)')"
if [ "$DRY_RUN" = 1 ]; then
  note "would run: ${COMPOSE[*]} up -d --wait --wait-timeout 600$( [ "$BUILD" = 1 ] && echo ' --build')"
else
  "${COMPOSE[@]}" up -d --wait --wait-timeout 600 $([ "$BUILD" = 1 ] && echo --build) \
    || {
      warn "compose failed or timed out. Recent logs:"
      "${COMPOSE[@]}" logs --tail 40 backend || true
      die "deployment failed"
    }
fi

# ---------------------------------------------------------------- 6. verify + hand off
if [ "$DRY_RUN" != 1 ]; then
  say "health check"
  for _ in $(seq 1 30); do
    if curl -fsS -m 5 "https://${DOMAIN_VAL}/api/v1/utils/health-check/" >/dev/null 2>&1; then
      printf '    https://%s -> healthy\n' "$DOMAIN_VAL"
      break
    fi
    sleep 2
  done
  # the internal check tells us whether the app is up even if TLS is not ready yet
  if ! curl -fsS -m 5 "https://${DOMAIN_VAL}/api/v1/utils/health-check/" >/dev/null 2>&1; then
    warn "no HTTPS response yet. Certificate issuance can take a minute; check with:"
    note "docker compose -f compose.yml -f compose.deploy.yml logs -f proxy backend"
    note "curl -s http://localhost/api/v1/utils/health-check/   # bypassing Traefik"
  fi

  ADMIN_EMAIL="$(get_env FIRST_SUPERUSER)"
  cat <<EOF

${c_grn}==> deployed${c_off}  https://${DOMAIN_VAL}

  Sign in with ${ADMIN_EMAIL} and the FIRST_SUPERUSER_PASSWORD in .env.

  Register one worker PER SERVICE (each key is shown once). One worker runs one
  service: the broker stores a single service binding per worker, so a worker that
  announces two ends up bound to whichever was published last and silently ignores
  the rest of the queue.

    for name in lan-sgf lan-intonation; do
      curl -s -X POST https://${DOMAIN_VAL}/api/v1/admin/workers/ \\
        -H "Authorization: Bearer \$TOKEN" -H 'Content-Type: application/json' \\
        -d "{\\"name\\":\\"\$name\\"}"
    done

  Then on the LAN machine, with the interpreter each service needs:

    # sgf_video -- toolkit venv. --toolkit names the checkout's sgf_tools/: the pipeline
    # now ships in services/sgf_video/, so this is where the steps it shares with
    # non-service tools live (remote_katago.py, sgf_animate.py). Plus KataGo/ACE-Step.
    .venv/bin/python worker.py --broker https://${DOMAIN_VAL}/api/v1 \\
      --key <key-sgf> --name lan-sgf --service sgf_video \\
      --plugin-dir /path/to/selfCommentary/services \\
      --toolkit /path/to/selfCommentary/sgf_tools

    # intonationCheck -- its own venv; no toolkit, KataGo or ACE-Step
    .venv-intonation/bin/python worker.py --broker https://${DOMAIN_VAL}/api/v1 \\
      --key <key-intonation> --name lan-intonation --service intonationCheck \\
      --plugin-dir /path/to/selfCommentary/services

  Adminer is NOT running (it is behind a compose profile, deliberately -- it has no
  auth and would otherwise be public). Add --profile debug to compose to bring it up.

  Re-run this script after code changes:  ./deploy/deploy.sh --no-build
EOF
else
  say "dry run complete -- nothing was changed"
fi
