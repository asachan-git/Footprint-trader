"""ClaudeMode enum — controls how much Claude participates in decisions.

FULL       — Claude decides direction + composes full grid plan every primary-TF bar.
RESTRICTED — Mechanical pipeline decides direction; Claude composes grid plan on
             cycle init only (not per bar). ~10–250x cheaper than FULL.
OFF        — Pure mechanical; no Claude calls. Zero API cost.

Read from config/settings.yaml under `claude.mode`. Defaults to RESTRICTED.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml


class ClaudeMode(str, Enum):
    FULL       = "full"
    RESTRICTED = "restricted"
    OFF        = "off"


_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS = _ROOT / "config" / "settings.yaml"
_cached: ClaudeMode | None = None


def get_mode() -> ClaudeMode:
    """Read ClaudeMode from settings.yaml. Cached after first read."""
    global _cached
    if _cached is not None:
        return _cached
    try:
        cfg = yaml.safe_load(_SETTINGS.read_text())
        raw = (cfg.get("claude") or {}).get("mode", "restricted")
        _cached = ClaudeMode(str(raw).lower())
    except Exception:
        _cached = ClaudeMode.RESTRICTED
    return _cached


def refresh() -> ClaudeMode:
    """Force re-read from disk (e.g. after config hot-reload)."""
    global _cached
    _cached = None
    return get_mode()


def is_claude_active() -> bool:
    return get_mode() != ClaudeMode.OFF


def is_per_bar() -> bool:
    """True only in FULL mode — Claude called on every primary-TF bar close."""
    return get_mode() == ClaudeMode.FULL


def is_cycle_init_only() -> bool:
    """True in RESTRICTED mode — Claude called only when a new cycle is proposed."""
    return get_mode() == ClaudeMode.RESTRICTED
