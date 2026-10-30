import hashlib
import json
from typing import Any


def progress_fingerprint(state: dict[str, Any]) -> str:
    commands = state.get("command_results", [])[-2:]
    value = {
        "files_changed": sorted(set(state.get("files_changed", []))),
        "commands": commands,
        "tool_result_refs": state.get("tool_result_refs", [])[-3:],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
