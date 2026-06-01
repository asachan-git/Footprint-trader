"""Strategy registry — maps config names to Strategy classes.

Add a new strategy in two steps:
  1. Implement a Strategy subclass (e.g. strategies/my_strat.py).
  2. Register it in REGISTRY below and enable it in config/strategies.yaml.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .base import Strategy
from .democracy import Democracy
from .republic import Republic
from .coup import Coup

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_YAML = ROOT / "config" / "strategies.yaml"

# name → class. The single source of truth for what can be deployed.
REGISTRY: dict[str, type[Strategy]] = {
    "democracy": Democracy,
    "republic": Republic,
    "coup": Coup,
}


def load_config() -> dict:
    if not STRATEGIES_YAML.exists():
        return {"strategies": [{"name": "democracy", "enabled": True}]}
    return yaml.safe_load(STRATEGIES_YAML.read_text()) or {}


def build_enabled() -> list[Strategy]:
    """Instantiate every enabled, registered strategy from config."""
    cfg = load_config()
    out: list[Strategy] = []
    for entry in cfg.get("strategies", []):
        name = entry.get("name")
        if not entry.get("enabled", True):
            continue
        cls = REGISTRY.get(name)
        if cls is None:
            LOG.warning(f"[registry] unknown strategy {name!r} — skipping (not in REGISTRY)")
            continue
        out.append(cls(config=entry.get("config") or {}))
        LOG.info(f"[registry] loaded strategy: {name}")
    return out
