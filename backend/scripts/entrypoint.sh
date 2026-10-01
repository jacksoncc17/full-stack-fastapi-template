#!/usr/bin/env bash
# Container entrypoint: bring the database up to date, then hand off to the server.
#
# Why this exists. The image used to declare only `CMD ["fastapi", "run", ...]`, and
# nothing in the deploy path ran `scripts/prestart.sh` -- `grep -rn prestart deploy/
# compose*.yml` found it only in the README, which claimed it ran. The consequences on a
# fresh droplet were a container that came up looking healthy while the database had no
# tables at all:
#
#     sqlalchemy.exc.ProgrammingError: relation "user" does not exist
#
# and no first superuser, so login failed with a generic 400. Doing this in the
# entrypoint rather than as a compose `command` means it also applies to Dockerfile-only
# runs, to `docker compose run`, and to any future compose file -- there is nothing to
# remember.
#
# Both steps are safe to repeat on every start, which is what makes this viable:
#   * `alembic upgrade head` is a no-op once the database is current
#   * `initial_data.py` creates the superuser only if that email does not exist yet
#
# Deliberately not `exec`-ing prestart and then the server: prestart must finish and
# succeed first, and `set -e` is what turns a migration failure into a container that
# refuses to start rather than one that serves errors.
set -e

cd /app/backend

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
    echo "entrypoint: applying migrations and seeding initial data"
    bash scripts/prestart.sh
else
    echo "entrypoint: SKIP_MIGRATIONS=1, not touching the database"
fi

exec "$@"
