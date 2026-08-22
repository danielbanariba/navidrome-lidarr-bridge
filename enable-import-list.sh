#!/usr/bin/env bash
# Turn on the Lidarr import list once Navidrome credentials are in place.
#
# Lidarr validates a list's URL before it will store it as enabled, so the list
# can only be flipped on after the bridge answers 200 on /artists.json — which
# it does as soon as NAVIDROME_USER / NAVIDROME_PASS are set in .env.
set -euo pipefail

# Override any of these for a different layout:
#   COMPOSE=... LIDARR_CONFIG=... ./enable-import-list.sh
COMPOSE=${COMPOSE:-./docker-compose.yml}
LIDARR_CONFIG=${LIDARR_CONFIG:-./lidarr/config.xml}
LIDARR=${LIDARR:-http://localhost:8686}
BRIDGE=${BRIDGE:-http://localhost:8687}
LIST_ID=${LIST_ID:-1}

# The api key can be passed directly; otherwise it is read out of Lidarr's config.
KEY=${LIDARR_API_KEY:-$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$LIDARR_CONFIG")}
if [ -z "$KEY" ]; then
  echo "no Lidarr api key: set LIDARR_API_KEY or point LIDARR_CONFIG at config.xml" >&2
  exit 1
fi

docker compose -f "$COMPOSE" up -d navidrome-lidarr-bridge
echo "waiting for the bridge to complete a sync..."
for _ in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BRIDGE/artists.json") && [ "$code" = 200 ] && break
  sleep 2
done
if [ "${code:-}" != 200 ]; then
  echo "bridge still failing; /status says:" >&2
  curl -s "$BRIDGE/status" >&2
  exit 1
fi

curl -fsS -H "X-Api-Key: $KEY" "$LIDARR/api/v1/importlist/$LIST_ID" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); d["enableAutomaticAdd"]=True; print(json.dumps(d))' \
  | curl -fsS -X PUT -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
      --data @- "$LIDARR/api/v1/importlist/$LIST_ID" >/dev/null

echo "import list $LIST_ID enabled."
curl -s "$BRIDGE/status"
