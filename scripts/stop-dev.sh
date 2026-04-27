#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/runtime/logs"
PID_FILE="$LOG_DIR/jarvis-osm-lab.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "Nicht gestartet (kein PID-File)."
  exit 0
fi

pid="$(cat "$PID_FILE")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "Gestoppt: jarvis-osm-lab (PID $pid)"
else
  echo "Prozess $pid läuft nicht mehr."
fi
rm -f "$PID_FILE"
