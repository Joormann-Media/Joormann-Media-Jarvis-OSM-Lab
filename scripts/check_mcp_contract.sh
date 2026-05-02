#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-${PUBLIC_BASE_URL:-http://127.0.0.1:${FLASK_PORT:-5079}}}"
BASE_URL="${BASE_URL%/}"

need(){ command -v "$1" >/dev/null || { echo "missing command: $1" >&2; exit 2; }; }
need curl
need python3

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for path in /api/health /api/capabilities /api/manifest /api/mcp/actions /api/mcp/settings; do
  code="$(curl -sS -o "$tmp/${path//\//_}.json" -w '%{http_code}' "$BASE_URL$path" || true)"
  echo "$path -> $code"
  [[ "$code" =~ ^2 ]] || exit 1
done

python3 - "$tmp/_api_mcp_actions.json" <<'PY'
import json, sys
data=json.load(open(sys.argv[1], encoding="utf-8"))
actions=data.get("actions") or []
enabled=[a for a in actions if a.get("enabled")]
missing_perm=[a.get("name") or a.get("tool_name") or a.get("id") for a in actions if not (a.get("permission_key") or a.get("permission"))]
missing_schema=[a.get("name") or a.get("tool_name") or a.get("id") for a in actions if not isinstance(a.get("input_schema"), dict) or not isinstance(a.get("output_schema"), dict)]
not_readonly=[a.get("name") or a.get("tool_name") or a.get("id") for a in actions if str(a.get("name") or a.get("tool_name") or "").startswith("geo.") and not a.get("read_only")]
print(f"actions={len(actions)} enabled={len(enabled)}")
print("missing_permission_key=" + (",".join(filter(None, missing_perm)) or "-"))
print("missing_schema=" + (",".join(filter(None, missing_schema)) or "-"))
print("read_only_false_warnings=" + (",".join(filter(None, not_readonly)) or "-"))
sys.exit(1 if missing_perm or missing_schema or not_readonly else 0)
PY
