"""Redirectable data root, so a backtest cannot write into live trading state.

Every durable write the execution layer makes (audit logs, arm state, cycle
outcomes, VP cache, footprint bars) resolves through `data_dir()`. Set FB_DATA_DIR
and the whole lot moves to a scratch directory.

IMPORTANT — these are FUNCTIONS, not module-level constants, on purpose. The
constants they replaced were evaluated at import time, so setting FB_DATA_DIR after
the first import silently had no effect and the run would write into `data/`. That
failure is invisible until live state is already corrupted, so the ordering hazard
is designed out rather than documented around.
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    return _ROOT


def data_dir() -> Path:
    """Root for all durable data. Override with FB_DATA_DIR (backtest scratch)."""
    override = os.environ.get("FB_DATA_DIR")
    return Path(override) if override else _ROOT / "data"


def is_redirected() -> bool:
    return bool(os.environ.get("FB_DATA_DIR"))


def assert_scratch() -> None:
    """Hard-fail if writes would land in the live data dir.

    Called by the harness after setup. Given the blast radius of a backtest
    appending to live cycle logs or rewriting vp_cache.json, this fails loudly
    rather than trusting that the env var was set in the right order.
    """
    if not is_redirected():
        raise RuntimeError(
            "FB_DATA_DIR is not set — refusing to run: writes would hit live data/. "
            "Set FB_DATA_DIR to a scratch directory before importing execution modules."
        )
