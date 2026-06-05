"""Composed-strategy framework — strategies defined as a config wiring of
registered components (trigger, entry, sl, tp, trail, exits, execution) run by a
generic engine, instead of bespoke Strategy subclasses.

Goal: full config-driven DSL. A strategy is a YAML block naming components +
their params; ComposedStrategy resolves and runs them. Ports are validated
bit-for-bit against the legacy class they replace (scripts/parity_*.py).
"""
from . import components  # noqa: F401  (registers components on import)
from .engine import ComposedStrategy  # noqa: F401
