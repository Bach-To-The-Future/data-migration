#!/bin/sh
# The engine's checks, in the sandbox toolbox: lint, then types, then tests.
# Needs Docker running and the sandbox built (python corpus/build.py).
# Arguments go to pytest, for example: sh sandbox/engine-tests.sh engine/tests/test_layer1.py -q
set -e

if ! docker info >/dev/null 2>&1; then
    echo "engine-tests: Docker is not running. Start Docker Engine (or Docker Desktop), then run this again." >&2
    exit 2
fi

compose="$(dirname "$0")/docker-compose.yml"
# Sessions working in parallel (each in its own worktree) share one sandbox. Compose would
# recreate a running SQL Server or Postgres whose configuration differs, as it does when the
# compose file sits in another worktree, so leave running ones alone.
deps=""
if [ -n "$(docker ps -q --filter label=com.docker.compose.project=data-migration-sandbox         --filter label=com.docker.compose.service=sqlserver)" ]; then
    deps="--no-deps"
fi
exec docker compose -f "$compose" --profile tools run $deps --rm -T tools sh -c '
    set -e
    echo "== lint (pyflakes)"
    python -m pyflakes engine
    echo "== types (pyright)"
    pyright --project engine
    echo "== tests (pytest)"
    python -m pytest engine "$@"
' engine-tests "$@"
