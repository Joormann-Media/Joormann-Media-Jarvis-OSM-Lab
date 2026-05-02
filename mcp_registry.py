from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "config"
MCP_ACTIONS_PATH = CONFIG_DIR / "mcp_actions.local.json"


def mask_sensitive_data(data: Any) -> Any:
    sensitive = ("api_key", "apikey", "token", "secret", "password", "client_secret", "access_key")

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: ("***" if any(s in str(k).lower() for s in sensitive) else walk(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(data)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_mcp_actions() -> list[dict[str, Any]]:
    data = _read_json(MCP_ACTIONS_PATH, {"actions": []})
    actions = data.get("actions") if isinstance(data, dict) else []
    return actions if isinstance(actions, list) else []


def save_mcp_actions(actions: list[dict[str, Any]]) -> None:
    _write_json(MCP_ACTIONS_PATH, {"generated_at": int(time.time()), "actions": actions})


def permission_key_for(action: dict[str, Any]) -> str:
    existing = str(action.get("permission_key") or action.get("permission") or "").strip()
    if existing:
        return existing
    name = str(action.get("tool_name") or action.get("name") or action.get("id") or "").strip()
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8] if name else ""


def normalize_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in actions:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        permission = permission_key_for(row)
        row["permission"] = permission
        row["permission_key"] = permission
        row["read_only"] = bool(row.get("read_only", True))
        row["requires_confirmation"] = bool(row.get("requires_confirmation", False))
        row["dry_run_supported"] = bool(row.get("dry_run_supported", False))
        row["audit_enabled"] = bool(row.get("audit_enabled", True))
        row["input_schema"] = row.get("input_schema") if isinstance(row.get("input_schema"), dict) else {}
        row["output_schema"] = row.get("output_schema") if isinstance(row.get("output_schema"), dict) else {}
        row["tags"] = row.get("tags") if isinstance(row.get("tags"), list) else ["geo", "read"]
        row["phase"] = str(row.get("phase") or "readonly")
        row["risk_level"] = str(row.get("risk_level") or "low")
        row["http_method"] = str(row.get("http_method") or row.get("method") or "GET").upper()
        row["method"] = row["http_method"]
        out.append(row)
    return out


def export_enabled_mcp_tools(actions: list[dict[str, Any]]) -> dict[str, Any]:
    exported = [
        mask_sensitive_data(action)
        for action in normalize_actions(actions)
        if action.get("enabled") and action.get("read_only") and str(action.get("risk_level") or "").lower() != "dangerous"
    ]
    return {"generated_at": int(time.time()), "count": len(exported), "actions": exported}
