#!/bin/bash
# Reconcile Lidarr's wanted list against what Navidrome can actually see.
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
export NAVIDROME_URL=${AUDIT_NAVIDROME_URL:-http://localhost:4533}
export STATE_DIR=${AUDIT_STATE_DIR:-$ROOT/state}

# --apply, because a dry run on a timer is a log nobody reads. The tool's own
# guards are what make that safe: it exempts anything in requested.json, it
# never touches an album Lidarr already holds a file for, and an artist
# Navidrome cannot answer about is skipped rather than assumed empty.
exec "$ROOT/.venv/bin/python" "$ROOT/tools/reconcile-monitoring.py" --apply "$@"
