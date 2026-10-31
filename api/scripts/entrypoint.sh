#!/bin/sh
# Apply migrations, then hand over to the container command.
# M0 has no revisions, so `upgrade head` is a no-op that proves Alembic is wired.
set -eu

# The worker (docs/21) sets SKIP_MIGRATIONS=1: two services migrating the same
# database at the same moment can collide, so only the API does it.
if [ "${SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] SKIP_MIGRATIONS=1, not migrating"
else
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
fi

echo "[entrypoint] starting: $*"
exec "$@"
