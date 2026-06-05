"""Component registries for the composed-strategy framework.

Six kinds of component, each a `name -> factory` map. A factory takes the
component's config dict and returns a callable component instance. The engine
resolves the names from a strategy config and runs them in lifecycle order:

    trigger -> entry -> sl -> tp -> (per-bar) trail + exits

Component contracts (see components.py for concrete implementations + the
Signal/PlanDraft dataclasses they exchange):
  trigger(ctx)            -> Signal | None      # fires the entry
  entry(signal, ctx)      -> float              # fill price
  sl(signal, entry, ctx)  -> float              # stop price
  tp(signal, entry, sl, ctx) -> float | None    # target (None = reject trade)
  execution(plan, bar, ctx)  -> plan            # adjust_plan transform (leg shape, SL/TP clamp)
  exits                   -> dict               # cycle-flag contributions (settings.cycle.*)

Register with the @trigger("name") / @sl("name") ... decorators.
"""
from __future__ import annotations

from typing import Callable

TRIGGERS: dict[str, Callable] = {}
ENTRIES: dict[str, Callable] = {}
SL_RULES: dict[str, Callable] = {}
TP_RULES: dict[str, Callable] = {}
TRAILS: dict[str, Callable] = {}
EXITS: dict[str, Callable] = {}
EXECUTION: dict[str, Callable] = {}


def _reg(table: dict, name: str):
    def deco(fn):
        if name in table:
            raise ValueError(f"component {name!r} already registered in {table}")
        table[name] = fn
        return fn
    return deco


def trigger(name):   return _reg(TRIGGERS, name)
def entry(name):     return _reg(ENTRIES, name)
def sl_rule(name):   return _reg(SL_RULES, name)
def tp_rule(name):   return _reg(TP_RULES, name)
def trail(name):     return _reg(TRAILS, name)
def exit_rule(name): return _reg(EXITS, name)
def execution(name): return _reg(EXECUTION, name)


def resolve(table: dict, spec: dict | str, kind: str):
    """spec is either a name string or a {type: name, ...params} dict. Returns the
    factory-built component bound to its params."""
    if spec is None:
        return None
    if isinstance(spec, str):
        name, params = spec, {}
    else:
        params = dict(spec)
        name = params.pop("type", None)
    if name not in table:
        raise KeyError(f"unknown {kind} component {name!r}; have {sorted(table)}")
    return table[name](params)
