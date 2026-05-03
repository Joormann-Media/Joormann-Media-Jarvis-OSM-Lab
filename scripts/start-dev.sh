#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

CONFIG_DIR="$PROJECT_ROOT/config"
LOG_DIR="$PROJECT_ROOT/runtime/logs"
PID_FILE="$LOG_DIR/jarvis-osm-lab.pid"
LOG_FILE="$LOG_DIR/jarvis-osm-lab.log"

for env_file in "$CONFIG_DIR/ports.env" "$CONFIG_DIR/ports.local.env"; do
  [[ -f "$env_file" ]] && { set -a; source "$env_file"; set +a; }
done

FLASK_HOST="${FLASK_HOST:-0.0.0.0}"
FLASK_PORT="${FLASK_PORT:-5079}"
FLASK_DEBUG="${FLASK_DEBUG:-0}"

is_port_in_use() {
  python3 - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(); s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

mkdir -p "$LOG_DIR"
VENV_DIR="$PROJECT_ROOT/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Erstelle virtuelle Umgebung: $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
echo "Installiere/aktualisiere Requirements …"
"$PYTHON_BIN" -m pip install -q --upgrade pip
"$PYTHON_BIN" -m pip install -q -r "$PROJECT_ROOT/requirements.txt"

RUN_WITH_SG_DOCKER=0
if getent group docker >/dev/null 2>&1; then
  if id -nG | tr ' ' '\n' | grep -qx docker; then
    RUN_WITH_SG_DOCKER=0
  elif id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker \
       || getent group docker | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
    RUN_WITH_SG_DOCKER=1
  fi
fi

if [[ -f "$PID_FILE" ]]; then
  existing="$(cat "$PID_FILE")"
  if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
    echo "Bereits aktiv: jarvis-osm-lab (PID $existing)"
    echo "URL: http://${FLASK_HOST}:${FLASK_PORT}"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if is_port_in_use "$FLASK_PORT"; then
  echo "Port $FLASK_PORT belegt. Start abgebrochen."
  exit 1
fi

(
  cd "$PROJECT_ROOT"
  if [[ "$RUN_WITH_SG_DOCKER" == "1" ]]; then
    setsid -f sg docker -c "$(printf 'cd %q && exec env FLASK_HOST=%q FLASK_PORT=%q HOST=%q PORT=%q FLASK_DEBUG=%q %q app.py' \
      "$PROJECT_ROOT" "$FLASK_HOST" "$FLASK_PORT" "$FLASK_HOST" "$FLASK_PORT" "$FLASK_DEBUG" "$PYTHON_BIN")" >>"$LOG_FILE" 2>&1 </dev/null
  else
    setsid -f env FLASK_HOST="$FLASK_HOST" FLASK_PORT="$FLASK_PORT" HOST="$FLASK_HOST" PORT="$FLASK_PORT" FLASK_DEBUG="$FLASK_DEBUG" \
      "$PYTHON_BIN" app.py >>"$LOG_FILE" 2>&1 </dev/null
  fi
)

sleep 1
pid="$(pgrep -f "$PYTHON_BIN app.py" | tail -n 1 || true)"
[[ -n "$pid" ]] && echo "$pid" > "$PID_FILE"
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  echo "Gestartet: jarvis-osm-lab (PID $pid)"
  echo "URL:  http://${FLASK_HOST}:${FLASK_PORT}"
  echo "Log:  $LOG_FILE"
else
  rm -f "$PID_FILE"
  echo "Fehlgeschlagen. Siehe Log: $LOG_FILE"
  exit 1
fi
