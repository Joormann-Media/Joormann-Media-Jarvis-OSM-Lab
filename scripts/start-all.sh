#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OSM_COMPOSE_DIR="${OSM_COMPOSE_DIR:-/home/djanebmb/osm-neu}"
[[ -f "$PROJECT_ROOT/config/osm.env" ]] && { set -a; source "$PROJECT_ROOT/config/osm.env"; set +a; }

echo "==> Starte OSM Docker-Stack …"
if [[ -f "$OSM_COMPOSE_DIR/docker-compose.yml" ]]; then
  docker compose -f "$OSM_COMPOSE_DIR/docker-compose.yml" up -d
  echo "    Docker-Stack gestartet."
else
  echo "    WARNUNG: $OSM_COMPOSE_DIR/docker-compose.yml nicht gefunden – übersprungen."
fi

echo "==> Starte Flask App …"
"$SCRIPT_DIR/start-dev.sh"
