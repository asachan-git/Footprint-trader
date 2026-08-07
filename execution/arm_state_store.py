"""Crash-safe persistence for ExecBridge arm state.

Writes _last_arm and _last_emit to JSONL so Flask restarts don't orphan live
cycles. On startup, call load() to replay latest state per key back into
ExecBridge before the first poll arrives.

Format:
  arm_state.jsonl  — one record per set_last_arm call; keyed by (account, symbol, magic)
  emit_state.jsonl — one record per mark_emit/clear_emit call; keyed by (account, symbol, magic)

Both files are append-only. Only the LAST record per key is live state; older
records are history. Files are compacted (rewritten to latest-per-key only) once
they exceed ARM_COMPACT_THRESHOLD lines to keep load() fast.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import os

# jul09 imports execution.paths.data_dir here; that module does not exist on this branch,
# so the same FB_DATA_DIR-overridable resolution is inlined, matching the <repo>/data
# convention exec_bridge.py already uses for its audit logs. Resolved per call so an env
# override is honoured regardless of import order.
def data_dir() -> Path:
    override = os.environ.get("FB_DATA_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "data"


def _arm_log() -> Path:
    return data_dir() / "arm_state.jsonl"


def _emit_log() -> Path:
    return data_dir() / "emit_state.jsonl"

ARM_COMPACT_THRESHOLD  = 2000   # lines before compaction
EMIT_COMPACT_THRESHOLD = 500

_lock = threading.Lock()


# ── helpers ────────────────────────────────────────────────────────────────────

def _arm_key(record: dict) -> tuple:
    return (str(record.get("account", "")),
            str(record.get("broker_symbol", "")),
            int(record.get("magic", 0)))


def _emit_key(record: dict) -> tuple:
    return (str(record.get("account", "")),
            str(record.get("symbol", "")),
            int(record.get("magic", 0)))


def _append(path: Path, record: dict) -> int:
    """Append one JSON line; return new line count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    # cheap line count: count newlines in file
    try:
        return path.read_text().count("\n")
    except Exception:
        return 0


def _compact(path: Path, key_fn) -> None:
    """Rewrite file keeping only the latest record per key."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    latest: dict[tuple, dict] = {}
    for line in lines:
        try:
            rec = json.loads(line)
            latest[key_fn(rec)] = rec
        except Exception:
            pass
    with path.open("w") as fh:
        for rec in latest.values():
            fh.write(json.dumps(rec) + "\n")


def _load_latest(path: Path, key_fn) -> dict[tuple, dict]:
    """Read file, return latest record per key."""
    result: dict[tuple, dict] = {}
    try:
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
                result[key_fn(rec)] = rec
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return result


# ── public API ─────────────────────────────────────────────────────────────────

def persist_arm(account: str, broker_symbol: str, magic: int, state: dict) -> None:
    """Write an arm state record to disk."""
    record = {"account": str(account), "broker_symbol": str(broker_symbol),
              "magic": int(magic), **state}
    with _lock:
        n = _append(_arm_log(), record)
        if n >= ARM_COMPACT_THRESHOLD:
            _compact(_arm_log(), _arm_key)


def persist_emit(account: str, symbol: str, magic: int,
                 fulcrum: float | None) -> None:
    """Write an emit dedup record to disk. fulcrum=None means cleared."""
    record = {"account": str(account), "symbol": str(symbol),
              "magic": int(magic), "fulcrum": fulcrum}
    with _lock:
        n = _append(_emit_log(), record)
        if n >= EMIT_COMPACT_THRESHOLD:
            _compact(_emit_log(), _emit_key)


def load() -> tuple[dict[tuple, dict], dict[tuple, float | None]]:
    """Load persisted state on startup.

    Returns:
        arms  — {(account, broker_symbol, magic): state_dict}
        emits — {(account, symbol, magic): fulcrum_or_None}
    """
    with _lock:
        raw_arms  = _load_latest(_arm_log(),  _arm_key)
        raw_emits = _load_latest(_emit_log(), _emit_key)

    arms: dict[tuple, dict] = {}
    for key, rec in raw_arms.items():
        state = {k: v for k, v in rec.items()
                 if k not in ("account", "broker_symbol", "magic")}
        # KEEP magic in the body (2026-08-05 — deliberately differs from the jul09 original,
        # which strips it as "redundant with the outer key"). On THIS branch
        # ExecBridge.set_last_arm derives its key from `magic or meta["magic"]`, and
        # monitor_cycle re-saves the cycle with bare `**cyc` spreads that pass no explicit
        # magic=. A restored, magic-less cyc would therefore collapse to magic=0 and
        # overwrite a DIFFERENT cycle's slot (bias_peak / flatten_ts silently vanishing).
        # Re-injecting here kills that whole failure class instead of relying on every
        # call site to remember.
        state["magic"] = int(rec.get("magic", 0) or 0)
        arms[key] = state

    emits: dict[tuple, float | None] = {}
    for key, rec in raw_emits.items():
        emits[key] = rec.get("fulcrum")

    return arms, emits
