#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PROJECT_ROOT/config"
PORTS_ENV_FILE="${JARVIS_PORTS_FILE:-$CONFIG_DIR/ports.env}"
PORTS_LOCAL_FILE="${JARVIS_PORTS_LOCAL_FILE:-$CONFIG_DIR/ports.local.env}"

if [[ -f "$PORTS_ENV_FILE" ]]; then
  set -a; source "$PORTS_ENV_FILE"; set +a
fi
if [[ -f "$PORTS_LOCAL_FILE" ]]; then
  set -a; source "$PORTS_LOCAL_FILE"; set +a
fi
if [[ -f "$CONFIG_DIR/osm.env" ]]; then
  set -a; source "$CONFIG_DIR/osm.env"; set +a
fi

PORTAL_AUTODISCOVER_URL="${PORTAL_AUTODISCOVER_URL:-${AUTODISCOVER_URL:-}}"
if [[ -z "$PORTAL_AUTODISCOVER_URL" ]]; then
  PORTAL_HOST="${PORTAL_HOST:-127.0.0.1}"
  PORTAL_PORT="${PORTAL_PORT:-5070}"
  PORTAL_AUTODISCOVER_URL="http://${PORTAL_HOST}:${PORTAL_PORT}/autodiscover"
fi

if ! command -v curl >/dev/null 2>&1; then
  exit 0
fi

repo_name="$(basename "$PROJECT_ROOT")"
repo_link=""
repo_branch="${AUTODISCOVER_REPO_BRANCH:-main}"
if command -v git >/dev/null 2>&1; then
  repo_link="$(git -C "$PROJECT_ROOT" remote get-url origin 2>/dev/null || true)"
  branch_guess="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ -n "$branch_guess" && "$branch_guess" != "HEAD" ]]; then
    repo_branch="$branch_guess"
  fi
fi
if [[ "$repo_link" =~ ^git@github.com:(.+)$ ]]; then
  repo_link="https://github.com/${BASH_REMATCH[1]}"
fi
if [[ -n "$repo_link" && ! "$repo_link" =~ \.git$ ]]; then
  repo_link="${repo_link}.git"
fi

service_name="${AUTODISCOVER_SERVICE_NAME:-jarvis-osm-lab.service}"
service_user="${AUTODISCOVER_SERVICE_USER:-${USER:-}}"
install_dir="${AUTODISCOVER_INSTALL_DIR:-$PROJECT_ROOT}"
service_port="${AUTODISCOVER_PORT:-${FLASK_PORT:-5079}}"
lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
host_ip="${AUTODISCOVER_HOST_IP:-${lan_ip:-127.0.0.1}}"

api_base_url="${AUTODISCOVER_API_BASE_URL:-http://${host_ip}:${service_port}}"
health_url="${AUTODISCOVER_HEALTH_URL:-${api_base_url}/health}"
ui_url="${AUTODISCOVER_UI_URL:-${api_base_url}/}"
tileserver_url="${TILESERVER_URL:-http://localhost:8083}"
nominatim_url="${NOMINATIM_URL:-http://localhost:7071}"
graphhopper_url="${GRAPHHOPPER_URL:-http://localhost:8989}"
overpass_url="${OVERPASS_URL:-http://localhost:7072}"
osm_data_root="${OSM_DATA_ROOT:-}"

node_name="${AUTODISCOVER_NODE_NAME:-$(hostname 2>/dev/null || true)}"
instance_id="${AUTODISCOVER_INSTANCE_ID:-${node_name}-${repo_name}}"

payload="$(python3 - <<PY
import json

print(json.dumps({
  "repo_name": ${repo_name@Q},
  "repo_link": ${repo_link@Q},
  "repo_branch": ${repo_branch@Q},
  "install_dir": ${install_dir@Q},
  "service_name": ${service_name@Q},
  "service_user": ${service_user@Q},
  "use_service": True,
  "autostart": True,
  "service_port": int(${service_port@Q}),
  "api_base_url": ${api_base_url@Q},
  "health_url": ${health_url@Q},
  "ui_url": ${ui_url@Q},
  "endpoints": {
    "api_base": ${api_base_url@Q},
    "health": ${health_url@Q},
    "ui": ${ui_url@Q},
    "manifest": ${api_base_url@Q} + "/api/service-manifest",
    "capabilities": ${api_base_url@Q} + "/api/capabilities",
    "geocode": ${api_base_url@Q} + "/api/geocode",
    "reverse": ${api_base_url@Q} + "/api/reverse",
    "route": ${api_base_url@Q} + "/api/route",
    "poi": ${api_base_url@Q} + "/api/poi",
  },
  "hostname": ${node_name@Q},
  "node_name": ${node_name@Q},
  "instance_id": ${instance_id@Q},
  "tags": ["jarvis", "autodiscover", "osm", "maps", "routing", "geocoding"],
  "capabilities": [
    "geo.geocode",
    "geo.reverse_geocode",
    "map.route_plan",
    "map.route_plan.multistop",
    "map.poi_search",
    "jarvis.routing.osm_lab",
  ],
  "metadata": {
    "tileserver_url": ${tileserver_url@Q},
    "nominatim_url": ${nominatim_url@Q},
    "graphhopper_url": ${graphhopper_url@Q},
    "overpass_url": ${overpass_url@Q},
    "osm_data_root": ${osm_data_root@Q},
  },
}))
PY
)"

curl -fsS --max-time 4 --connect-timeout 2 \
  -H "Content-Type: application/json" \
  -X POST "$PORTAL_AUTODISCOVER_URL" \
  --data-binary "$payload" >/dev/null 2>&1 || true
