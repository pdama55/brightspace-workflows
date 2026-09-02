"""Tiny JSON state file so the loop does not re-text the same digest."""
from __future__ import annotations

import json
from typing import Any, Dict

from .config import STATE_PATH


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
