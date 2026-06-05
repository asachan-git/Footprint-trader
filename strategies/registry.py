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
from .senate import Senate
from .congress import Congress
from .coup import Coup
from .coup_reversal import CoupReversal
from .reversal import Reversal
from .reversal_si import ReversalSI
from .reversal_choch import ReversalChoch
from .wave_fib import WaveFib

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
STRATEGIES_YAML = ROOT / "config" / "strategies.yaml"

# name → class. The single source of truth for what can be deployed.
REGISTRY: dict[str, type[Strategy]] = {
    "democracy": Democracy,
    "republic": Republic,
    "senate": Senate,
    "congress": Congress,
    "coup": Coup,
    "coup_reversal": CoupReversal,
    "reversal": Reversal,
    "reversal_si": ReversalSI,
    "reversal_choch": ReversalChoch,
    "wave_fib": WaveFib,
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
        # `class` lets a config entry instantiate a registered class under a
        # different instance name (e.g. name=coup_5m, class=coup) so a 5m and a 15m
        # instance get separate stores (data/strategies/<name>/). Falls back to name.
        # engine: composed → the config-driven ComposedStrategy (components wired
        # in config), instead of a bespoke registered class.
        if entry.get("engine") == "composed":
            from .composed.engine import ComposedStrategy
            inst = ComposedStrategy(config={**(entry.get("config") or {}), "name": name})
            inst.name = name
            out.append(inst)
            LOG.info(f"[registry] loaded composed strategy: {name}")
            continue
        cls = REGISTRY.get(entry.get("class") or name)
        if cls is None:
            LOG.warning(f"[registry] unknown strategy {name!r} — skipping (not in REGISTRY)")
            continue
        inst = cls(config=entry.get("config") or {})
        inst.name = name          # per-instance store/log key (overrides class attr)
        out.append(inst)
        LOG.info(f"[registry] loaded strategy: {name}")
    return out
