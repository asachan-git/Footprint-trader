"""Bar store keyed by (symbol, tf, close_ts) with as-of query.

Append-only JSONL persistence so labeling can run in a separate process.
On startup, reload all bars from disk so a fresh `python scripts/run_replay.py`
sees the bars that the server captured.
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from .types import Bar, Level, OHLC

PERSIST_DIR = Path(__file__).resolve().parent.parent / "data" / "footprint"


def _path(symbol: str, tf: str) -> Path:
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    return PERSIST_DIR / f"{symbol}_{tf}.jsonl"


def _serialize(bar: Bar) -> str:
    d = asdict(bar)
    return json.dumps(d, separators=(",", ":"))


_LEVEL_FIELDS = {"price", "vol", "cnt"}


def _level(lvl: dict) -> "Level":
    """Build a Level, DROPPING keys this build does not know.

    A newer writer plus an older reader must degrade, not brick the store. The
    footprint files are append-only and shared across branches, so a field added
    on one branch (Level.cnt, 2026-07-07) made every older tree crash on load
    with a bare TypeError — the server could not boot at all. Unknown keys are
    dropped rather than raising; missing known keys keep their defaults."""
    return Level(**{k: v for k, v in lvl.items() if k in _LEVEL_FIELDS})


def _deserialize(line: str) -> Bar:
    d = json.loads(line)
    return Bar(
        bar_id=d["bar_id"],
        symbol=d["symbol"],
        tf=d["tf"],
        close_ts=d["close_ts"],
        source=d["source"],
        ohlc=OHLC(**d["ohlc"]),
        bid_ladder=tuple(_level(lvl) for lvl in d["bid_ladder"]),
        ask_ladder=tuple(_level(lvl) for lvl in d["ask_ladder"]),
        poc=d.get("poc"),
        delta=d.get("delta"),
    )


class StateStore:
    def __init__(self) -> None:
        self._bars: dict[tuple[str, str], list[Bar]] = defaultdict(list)
        self._lock = Lock()
        self._load()

    def _load(self) -> None:
        if not PERSIST_DIR.exists():
            return
        for f in PERSIST_DIR.glob("*.jsonl"):
            with f.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    bar = _deserialize(line)
                    self._bars[(bar.symbol, bar.tf)].append(bar)
        for series in self._bars.values():
            series.sort(key=lambda b: b.close_ts)

    # Reject bars whose close_ts is a sentinel / far-future placeholder. A forming-bar
    # or test fixture with a max ts (e.g. 9999999999) would sort LAST and become the
    # "newest" bar, poisoning every raw recent()/latest() consumer — ATR read a 96pt
    # phantom range from such a bar (2026-07-15, bar_id ...|9999999999|cnttest, a leaked
    # count-VP test fixture that put() persisted to the live footprint file → oversized
    # 1m step → zeroed TP). Guard at the single write chokepoint so it can never persist.
    _MAX_VALID_TS = 9_000_000_000   # ~year 2255; real epoch-seconds bars are far below

    def put(self, bar: Bar) -> bool:
        """Idempotent insert. Returns True if new, False if duplicate/invalid bar_id."""
        _ts = getattr(bar, "close_ts", 0) or 0
        if _ts <= 0 or _ts >= self._MAX_VALID_TS:
            import logging
            logging.getLogger("state_store").warning(
                "[state_store] rejected sentinel/invalid-ts bar %s (close_ts=%s)",
                getattr(bar, "bar_id", "?"), _ts)
            return False
        with self._lock:
            series = self._bars[(bar.symbol, bar.tf)]
            for b in reversed(series):
                if b.bar_id == bar.bar_id:
                    return False
            bisect.insort(series, bar, key=lambda b: b.close_ts)
            with _path(bar.symbol, bar.tf).open("a") as fh:
                fh.write(_serialize(bar) + "\n")
            return True

    def latest(self, symbol: str, tf: str) -> Bar | None:
        series = self._bars.get((symbol, tf))
        return series[-1] if series else None

    def as_of(self, symbol: str, tf: str, ts: int) -> Bar | None:
        """Most recent bar with close_ts <= ts."""
        series = self._bars.get((symbol, tf))
        if not series:
            return None
        idx = bisect.bisect_right([b.close_ts for b in series], ts) - 1
        return series[idx] if idx >= 0 else None

    def recent(self, symbol: str, tf: str, n: int) -> list[Bar]:
        return list(self._bars.get((symbol, tf), [])[-n:])


_singleton: StateStore | None = None
_singleton_lock = Lock()


def store() -> StateStore:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = StateStore()
    return _singleton
