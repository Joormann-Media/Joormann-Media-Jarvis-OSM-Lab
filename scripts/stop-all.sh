#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OSM_COMPOSE_DIR="${OSM_COMPOSE_DIR:-$PROJECT_ROOT/docker}"
[[ -f "$PROJECT_ROOT/config/osm.env" ]] && { set -a; source "$PROJECT_ROOT/config/osm.env"; set +a; }

echo "==> Stoppe Flask App …"
"$SCRIPT_DIR/stop-dev.sh"

echo "==> Stoppe OSM Docker-Stack …"
if [[ -f "$OSM_COMPOSE_DIR/docker-compose.yml" ]]; then
  docker compose -f "$OSM_COMPOSE_DIR/docker-compose.yml" down
  echo "    Docker-Stack gestoppt."
else
  echo "    WARNUNG: $OSM_COMPOSE_DIR/docker-compose.yml nicht gefunden – übersprungen."
fi
