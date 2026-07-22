"""Injectable wall-clock — the single time source for the execution lifecycle.

Live code calls `clock.now()` where it used to call `time.time()`. With no source
installed this IS `time.time()`, so behaviour is byte-identical in production; the
backtest harness installs a simulated source and thereby drives cooldowns, daily
keys, reclaim windows and audit stamps off replay time instead of wall time.

Why a module seam instead of monkeypatching `time`: patching the global module also
warps lock timeouts, logging stamps and anything else that reads the clock, which
makes replay nondeterministic in ways that are very hard to attribute.

Scope note: `execution/grid_planner.py` has no clock calls — it is a pure function of
(bar store, VP cache, config). `execution/zone_triggers.py` was ALSO believed clock-free
when this module was written; that was wrong. `_cached_hvn`/`_cached_lvn` branch on the
IST hour via the aliased `_dt.now()`, which an initial grep for `time.time`/`datetime.now`
missed, and under replay they read the live hour and selected the wrong VP source. Both
now call clock.now(). Lesson: grep for aliased imports too before declaring a path
clock-free.
"""
from __future__ import annotations

import time
from typing import Callable

_now_fn: Callable[[], float] | None = None


def now() -> float:
    """Current epoch seconds — simulated if a source is installed, else wall time."""
    return _now_fn() if _now_fn is not None else time.time()


def set_source(fn: Callable[[], float]) -> None:
    """Install a time source. Harness-only — never call this from production paths."""
    global _now_fn
    _now_fn = fn


def reset() -> None:
    """Drop the installed source and fall back to wall time."""
    global _now_fn
    _now_fn = None


def is_simulated() -> bool:
    return _now_fn is not None
