"""Crash-safe persistence for ExecBridge arm state.

Writes _last_arm and _last_emit to JSONL so Flask restarts don't orphan live
cycles. On startup, call load() to replay latest state per key back into
ExecBridge before the first poll arrives.

Format:
  arm_state.jsonl  — one record per set_last_arm call; keyed by (account, symbol, magic)
  emit_state.jsonl — one record per mark_emit/clear_emit call; keyed by (account, symbol, magic)

Both files are append-only. Only the LAST record per key is live state; older
records are history. Files are compacted (rewritten to latest-per-key only) once
they exceed *_COMPACT_THRESHOLD lines to keep load() fast.

Ported from feat/backtest-seams 2026-07-24. The only adaptation: this branch has
no execution/paths.py::data_dir, so the path is _ROOT/"data" (the same convention
exec_bridge.py uses for exec_bridge.jsonl / cycle_outcomes.jsonl).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _arm_log() -> Path:
    return _ROOT / "data" / "arm_state.jsonl"


def _emit_log() -> Path:
    return _ROOT / "data" / "emit_state.jsonl"


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
        arms  — {(account, broker_symbol, magic): state_dict}   (magic stripped from body)
        emits — {(account, symbol, magic): fulcrum_or_None}
    """
    with _lock:
        raw_arms  = _load_latest(_arm_log(),  _arm_key)
        raw_emits = _load_latest(_emit_log(), _emit_key)

    arms: dict[tuple, dict] = {}
    for key, rec in raw_arms.items():
        # Strip the key components back out of the body. This is why every internal
        # set_last_arm(**cyc) spread MUST pass magic= explicitly — a restored cyc has
        # no embedded magic, so a bare spread would collapse the write to magic=0.
        state = {k: v for k, v in rec.items()
                 if k not in ("account", "broker_symbol", "magic")}
        arms[key] = state

    emits: dict[tuple, float | None] = {}
    for key, rec in raw_emits.items():
        emits[key] = rec.get("fulcrum")

    return arms, emits
