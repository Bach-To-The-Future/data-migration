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
exec docker compose -f "$compose" --profile tools run --rm -T tools sh -c '
    set -e
    echo "== lint (pyflakes)"
    python -m pyflakes engine
    echo "== types (pyright)"
    pyright --project engine
    echo "== tests (pytest)"
    python -m pytest engine "$@"
' engine-tests "$@"
