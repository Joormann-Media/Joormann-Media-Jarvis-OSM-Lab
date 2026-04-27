#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SYSTEMD_DIR="$PROJECT_ROOT/systemd"
SERVICE_TEMPLATE="$SYSTEMD_DIR/joormann-media-jarvis-osm-lab.service"
SERVICE_NAME="joormann-media-jarvis-osm-lab.service"
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
SERVICE_USER="${1:-${SUDO_USER:-$USER}}"

if [[ ! -f "$SERVICE_TEMPLATE" ]]; then
  echo "Service-Template fehlt: $SERVICE_TEMPLATE"; exit 1
fi
if [[ -z "$SERVICE_USER" ]]; then
  echo "Service-User fehlt."; exit 1
fi

TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed "s|__SERVICE_USER__|$SERVICE_USER|g; s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
  "$SERVICE_TEMPLATE" > "$TMP_UNIT"

sudo install -m 644 "$TMP_UNIT" "$SERVICE_PATH"
sudo systemctl daemon-reload

echo "Installiert:  $SERVICE_NAME"
echo "Service-User: $SERVICE_USER"
echo "Aktivieren:   $SCRIPT_DIR/service-enable.sh"
echo "Starten:      $SCRIPT_DIR/service-start.sh"
