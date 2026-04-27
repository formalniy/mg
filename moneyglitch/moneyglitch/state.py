"""Shared trading state persisted as JSON.

Bot writes; parser reads. Atomic via tmp+rename so a half-written file
can never be observed by the parser.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

STATE_PATH = Path(os.environ.get("MONEYGLITCH_STATE", "state.json"))

DEFAULT_STATE: Dict[str, Any] = {
    "amount_usd": 10.0,
    "leverage": 10,
    "stop_loss_pct": 5.0,
    "enabled": False,
}

_lock = threading.Lock()


def load_state() -> Dict[str, Any]:
    with _lock:
        if not STATE_PATH.exists():
            _write(DEFAULT_STATE)
            return dict(DEFAULT_STATE)
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        merged = dict(DEFAULT_STATE)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
        return merged


def save_state(state: Dict[str, Any]) -> None:
    with _lock:
        merged = dict(DEFAULT_STATE)
        merged.update({k: v for k, v in state.items() if k in DEFAULT_STATE})
        _write(merged)


def _write(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)
