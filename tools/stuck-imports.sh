#!/bin/bash
# Report stuck imports from the host.
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
# DATA_DIR is docker-compose's own interpolation variable for the download
# tree's bind mount (deploy/docker-compose.yml), not something the bridge
# itself reads — so it lives in deploy/.env rather than here. Picked up too,
# if that file exists, so the cue-sheet check can list outputPath the same
# way the containers see it. Harmless if the file or the variable is absent:
# the check just falls back to reporting by status message instead.
# shellcheck disable=SC1091
[ -f ./deploy/.env ] && . ./deploy/.env
set +a

# The same services, by the address the host reaches them at.
export LIDARR_URL=${AUDIT_LIDARR_URL:-http://localhost:8686}
export STATE_DIR=${AUDIT_STATE_DIR:-$ROOT/state}

# Neither .env is guaranteed to carry DATA_DIR: compose interpolates it from
# whatever shell ran `docker compose up`, and that shell may be long gone.
# Docker still knows where it mounted the tree, and asking it is right on any
# machine — where hardcoding the answer from one of them would not be. A stack
# that is down answers nothing, DATA_DIR stays empty, and the cue-sheet check
# degrades to reporting by status message, which host_path() documents.
: "${DATA_DIR:=$(docker inspect lidarr \
    --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || true)}"
export DATA_DIR

exec "$ROOT/.venv/bin/python" "$ROOT/tools/stuck-imports.py" "$@"
