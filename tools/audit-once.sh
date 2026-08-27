#!/bin/bash
# Run one audition from the host.
#
# The .env in this directory is written for the bridge, which runs in Docker and
# reaches its neighbours by container name. From the host those names do not
# resolve, and systemd cannot simply be told to override them: EnvironmentFile
# is read immediately before the process starts, so it wins over Environment=
# whatever order they appear in the unit. Sourcing the file and then exporting
# the host's own addresses is the one order that works.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

set -a
# shellcheck disable=SC1091
. ./.env
set +a

# The same services, by the address the host reaches them at.
export LIDARR_URL=${AUDIT_LIDARR_URL:-http://localhost:8686}
export PROWLARR_URL=${AUDIT_PROWLARR_URL:-http://localhost:9696}
export QBIT_URL=${AUDIT_QBIT_URL:-http://localhost:8090}
export NAVIDROME_URL=${AUDIT_NAVIDROME_URL:-http://localhost:4533}
export STATE_DIR=${AUDIT_STATE_DIR:-$ROOT/state}

exec "$ROOT/.venv/bin/python" "$ROOT/tools/audit-queue.py" "$@"
