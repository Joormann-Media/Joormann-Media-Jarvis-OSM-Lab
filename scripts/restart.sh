#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

printf "[restart] Stoppe Modul ...\n"
if [[ -x "$SCRIPT_DIR/stop-all.sh" ]]; then
  "$SCRIPT_DIR/stop-all.sh" || true
elif [[ -x "$SCRIPT_DIR/stop-dev.sh" ]]; then
  "$SCRIPT_DIR/stop-dev.sh" || true
fi

sleep 1

printf "[restart] Starte Modul ...\n"
if [[ -x "$SCRIPT_DIR/start-dev.sh" ]]; then
  exec "$SCRIPT_DIR/start-dev.sh"
elif [[ -x "$SCRIPT_DIR/start-all.sh" ]]; then
  exec "$SCRIPT_DIR/start-all.sh"
else
  echo "[restart] Kein Start-Script gefunden (start-dev.sh/start-all.sh)." >&2
  exit 1
fi
