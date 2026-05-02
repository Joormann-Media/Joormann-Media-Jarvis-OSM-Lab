from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent / "runtime"
LOG_PATH = RUNTIME_DIR / "logs" / "mcp_audit.local.jsonl"


def write_mcp_audit(event: str, payload: dict[str, Any] | None = None) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": int(time.time()), "event": event, "payload": payload or {}}
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
