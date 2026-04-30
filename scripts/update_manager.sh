#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-status}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$(mktemp)"
trap 'rm -f "$LOG_FILE"' EXIT

log() { printf '%s\n' "$*" | tee -a "$LOG_FILE" >&2; }

json_out() {
  local ok="$1"; shift
  local code="$1"; shift
  local message="$1"; shift
  local update_available="$1"; shift
  local local_commit="$1"; shift
  local remote_commit="$1"; shift
  local branch="$1"; shift
  local ahead="$1"; shift
  local behind="$1"; shift
  local restarted="$1"; shift
  python3 - "$LOG_FILE" "$ok" "$code" "$message" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" "$restarted" <<'PY'
import json
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
ok = sys.argv[2].lower() in {"1", "true", "yes", "on"}
code = sys.argv[3]
message = sys.argv[4]
update_available = sys.argv[5].lower() in {"1", "true", "yes", "on"}
local_commit = sys.argv[6]
remote_commit = sys.argv[7]
branch = sys.argv[8]
ahead = int(sys.argv[9] or 0)
behind = int(sys.argv[10] or 0)
restarted = sys.argv[11].lower() in {"1", "true", "yes", "on"}
log = ""
try:
    log = log_path.read_text(encoding="utf-8")
except Exception:
    pass
print(json.dumps({
    "ok": ok,
    "code": code,
    "message": message,
    "update_available": update_available,
    "local_commit": local_commit,
    "remote_commit": remote_commit,
    "branch": branch,
    "ahead": ahead,
    "behind": behind,
    "restarted": restarted,
    "log": log,
}, ensure_ascii=False))
PY
}

cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  json_out false not_git_repo "Kein Git-Repository" false "" "" "" 0 0 false
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
local_commit="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
remote_ref="origin/${branch}"
remote_commit=""
ahead=0
behind=0
update_available=false

log "[update] fetch origin"
if ! git fetch origin --prune >>"$LOG_FILE" 2>&1; then
  log "[warn] git fetch failed"
fi

if git rev-parse --verify "$remote_ref" >/dev/null 2>&1; then
  remote_commit="$(git rev-parse --short=12 "$remote_ref" 2>/dev/null || true)"
  counts="$(git rev-list --left-right --count HEAD..."$remote_ref" 2>/dev/null || echo '0 0')"
  ahead="$(printf '%s' "$counts" | awk '{print $1}')"
  behind="$(printf '%s' "$counts" | awk '{print $2}')"
  if [[ "${behind}" =~ ^[0-9]+$ ]] && (( behind > 0 )); then
    update_available=true
  fi
else
  log "[warn] remote ref not found: $remote_ref"
fi

if [[ "$MODE" == "status" ]]; then
  msg="Up-to-date"
  if [[ "$update_available" == "true" ]]; then
    msg="Update vorhanden"
  fi
  json_out true ok "$msg" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" false
  exit 0
fi

if [[ "$MODE" != "apply" ]]; then
  json_out false invalid_mode "Ungültiger Modus: $MODE" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" false
  exit 2
fi

log "[update] git pull --ff-only"
if ! git pull --ff-only >>"$LOG_FILE" 2>&1; then
  json_out false git_pull_failed "git pull fehlgeschlagen" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" false
  exit 1
fi

if [[ -x ".venv/bin/pip" && -f "requirements.txt" ]]; then
  log "[update] install requirements"
  if ! .venv/bin/pip install -r requirements.txt >>"$LOG_FILE" 2>&1; then
    json_out false requirements_failed "Requirements-Installation fehlgeschlagen" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" false
    exit 1
  fi
fi

restarted=false
if [[ -x "scripts/stop-all.sh" && -x "scripts/start-all.sh" ]]; then
  log "[update] restart via stop-all/start-all"
  if bash scripts/stop-all.sh >>"$LOG_FILE" 2>&1 && bash scripts/start-all.sh >>"$LOG_FILE" 2>&1; then
    restarted=true
  else
    log "[warn] restart via stop/start failed"
  fi
elif [[ -x "scripts/service-restart.sh" ]]; then
  log "[update] restart via service-restart.sh"
  if bash scripts/service-restart.sh >>"$LOG_FILE" 2>&1; then
    restarted=true
  else
    log "[warn] service restart failed"
  fi
fi

local_commit="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
if git rev-parse --verify "$remote_ref" >/dev/null 2>&1; then
  remote_commit="$(git rev-parse --short=12 "$remote_ref" 2>/dev/null || true)"
  counts="$(git rev-list --left-right --count HEAD..."$remote_ref" 2>/dev/null || echo '0 0')"
  ahead="$(printf '%s' "$counts" | awk '{print $1}')"
  behind="$(printf '%s' "$counts" | awk '{print $2}')"
  update_available=$([[ "${behind}" =~ ^[0-9]+$ ]] && (( behind > 0 )) && echo true || echo false)
fi

json_out true ok "Update abgeschlossen" "$update_available" "$local_commit" "$remote_commit" "$branch" "$ahead" "$behind" "$restarted"
