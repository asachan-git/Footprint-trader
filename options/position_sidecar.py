"""Sidecar store for option-specific metadata that can't go in GridPosition.

Maps position_id → {security_id, quantity, product_type, option_type, strike, expiry}.
Written by DhanAdapter after successful order. Read by monitor + close logic.

Persisted as JSON at data/options_positions.json. Thread-safe via lock.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_PATH = Path(__file__).resolve().parent.parent / "data" / "options_positions.json"
_lock = threading.Lock()


import logging as _logging
_LOG = _logging.getLogger(__name__)


def _load() -> dict[str, Any]:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text())
    except Exception as e:
        _LOG.error(f"[sidecar] corrupt JSON at {_PATH}: {e} — returning empty, positions may be lost")
        return {}


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2))


def write(position_id: str, meta: dict) -> None:
    """Store option metadata for position_id."""
    with _lock:
        data = _load()
        data[position_id] = meta
        _save(data)


def read(position_id: str) -> dict | None:
    with _lock:
        return _load().get(position_id)


def remove(position_id: str) -> None:
    with _lock:
        data = _load()
        data.pop(position_id, None)
        _save(data)


def all_open() -> dict[str, dict]:
    with _lock:
        return dict(_load())
