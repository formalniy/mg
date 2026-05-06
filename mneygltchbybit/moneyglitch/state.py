"""Per-account trading state persisted as JSON.

Bot writes settings; parser writes position open/close events. Atomic via
tmp+rename so a half-written file can never be observed.

Schema:
{
  "accounts": {
    "<name>": {
      "amount_usd": 10.0,
      "leverage": 10,
      "stop_loss_pct": 5.0,
      "enabled": false,
      "position": null | {
        "side": "Long",
        "symbol": "TONUSDT",
        "qty": "12.3",
        "entry_price": "1.9046",
        "sl_price": "1.81",
        "leverage": 10,
        "amount_usd": 10.0,
        "opened_at_ms": 1778012657749,
        "messages": [{"chat_id": 111, "message_id": 4321}, ...],
        "closing": false
      }
    }
  }
}

A legacy flat state file (DEFAULT keys at top level) is migrated on first
read into accounts["main"].
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_PATH = Path(os.environ.get("MONEYGLITCH_STATE", "state.json"))

DEFAULT_ACCOUNT: Dict[str, Any] = {
    "amount_usd": 10.0,
    "leverage": 10,
    "stop_loss_pct": 5.0,
    "enabled": False,
    "position": None,
}

_LEGACY_KEYS = {"amount_usd", "leverage", "stop_loss_pct", "enabled"}

_lock = threading.Lock()


def _read() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"accounts": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"accounts": {}}
    if not isinstance(data, dict):
        return {"accounts": {}}
    if "accounts" in data and isinstance(data["accounts"], dict):
        return data
    legacy = {k: data[k] for k in _LEGACY_KEYS if k in data}
    if legacy:
        return {"accounts": {"main": {**DEFAULT_ACCOUNT, **legacy}}}
    return {"accounts": {}}


def _write_root(root: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(root, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def load_account(name: str) -> Dict[str, Any]:
    with _lock:
        root = _read()
        accts = root.get("accounts") or {}
        out = dict(DEFAULT_ACCOUNT)
        out.update(accts.get(name) or {})
        return out


def save_account(name: str, state: Dict[str, Any]) -> None:
    """Replace the named account state, filling missing keys with defaults."""
    with _lock:
        root = _read()
        accts = root.setdefault("accounts", {})
        prev = accts.get(name) or {}
        accts[name] = {**DEFAULT_ACCOUNT, **prev, **state}
        _write_root(root)


def update_account(name: str, **fields: Any) -> Dict[str, Any]:
    """Partial update; returns the new account state."""
    with _lock:
        root = _read()
        accts = root.setdefault("accounts", {})
        prev = accts.get(name) or {}
        new = {**DEFAULT_ACCOUNT, **prev, **fields}
        accts[name] = new
        _write_root(root)
        return dict(new)


def get_position(name: str) -> Optional[Dict[str, Any]]:
    return load_account(name).get("position")


def set_position(name: str, position: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return update_account(name, position=position)


def patch_position(name: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Partial update of the position dict; no-op if no position."""
    with _lock:
        root = _read()
        accts = root.setdefault("accounts", {})
        prev = accts.get(name) or {}
        pos = prev.get("position")
        if not pos:
            return None
        merged = {**pos, **fields}
        accts[name] = {**DEFAULT_ACCOUNT, **prev, "position": merged}
        _write_root(root)
        return dict(merged)


def list_account_names() -> List[str]:
    with _lock:
        return list((_read().get("accounts") or {}).keys())
