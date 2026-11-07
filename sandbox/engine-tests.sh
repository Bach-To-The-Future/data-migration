#!/bin/sh
# Run the engine's tests in the sandbox toolbox: python corpus/build.py first.
exec docker compose -f "$(dirname "$0")/docker-compose.yml" --profile tools run --rm -T tools \
    python -m pytest engine "$@"
