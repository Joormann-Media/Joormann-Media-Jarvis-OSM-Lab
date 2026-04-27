#!/usr/bin/env bash
set -euo pipefail
SERVICE_NAME="joormann-media-jarvis-osm-lab.service"
sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
sudo systemctl disable "$SERVICE_NAME" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
echo "Deinstalliert: $SERVICE_NAME"
