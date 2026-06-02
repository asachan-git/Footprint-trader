#!/usr/bin/env python3
"""Build a labeled dataset of direction-engine votes + level context + forward
outcomes, for calibrating P(up) (plan Part 2.1).

For each historical 15m bar (no future leak via the same store.recent cutoff as
strategy_replay.py) we record:
  - every vote from collect_votes(): module, direction, strength, signed contrib
  - the aggregate decide_direction(): side, score, bias_strength
  - level context (best-effort): ATR-distance from close to daily VAH/VAL/POC +
    VA position enum  [CAVEAT: vp_cache daily levels are "as of now", not sliced
    to bar i — a mild look-ahead on the LEVEL features only; votes + outcomes are
    clean. Fine for a first calibration; revisit if level features prove useful.]
  - forward outcomes over HORIZON bars (clean): up/down, return %, MFE/MAE in ATR,
    whether VAH/VAL/POC were touched.

Output: data/strategies/vote_dataset.jsonl  (one row per bar per symbol).
Run: .venv/bin/python scripts/build_vote_dataset.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from pipeline.state_store import store
from pipeline.features.atr import atr
from execution.direction_engine import collect_votes, decide_direction
from pipeline.features import vp_cache

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TF = "15m"
WARMUP = 30
HORIZON = 6          # forward bars for the outcome label
OUT = ROOT / "data" / "strategies" / "vote_dataset.jsonl"

# ── store time-travel (votes/decide see only bars up to CUT) ──────────────────
S = store()
_orig = S.recent
CUT = {"ts": None}


def _recent(symbol, tf, n):
    bars = _orig(symbol, tf, 10_000_000)
    if CUT["ts"] is not None:
        bars = [b for b in bars if b.close_ts <= CUT["ts"]]
    return bars[-n:] if n else bars


S.recent = _recent


def _atr15(bars, upto):
    return atr(bars[max(0, upto - 14): upto + 1], period=14)


def _level_ctx(symbol, close, a):
    """ATR-distance to daily VAH/VAL/POC + VA position. Best-effort (see caveat)."""
    out = {}
    try:
        vp = vp_cache.get(symbol, "daily") or {}
    except Exception:
        vp = {}
    if a and a > 0:
        for key in ("vah", "val", "poc"):
            lvl = vp.get(key)
            if lvl:
                out[f"dist_{key}_atr"] = round((close - lvl) / a, 3)
    out["va_position"] = vp.get("current_position")
    return out


def _forward(bars, i, entry, a):
    """Outcome over HORIZON bars from bar i. Clean (uses only future bars)."""
    fut = bars[i + 1: i + 1 + HORIZON]
    if not fut or a <= 0:
        return None
    end = fut[-1].ohlc.c
    hi = max(b.ohlc.h for b in fut)
    lo = min(b.ohlc.l for b in fut)
    return {
        "fwd_up": end > entry,
        "fwd_ret_pct": round(100 * (end - entry) / entry, 4),
        "fwd_mfe_atr": round((hi - entry) / a, 3),
        "fwd_mae_atr": round((entry - lo) / a, 3),
    }


def build_symbol(symbol, rows):
    bars = _orig(symbol, TF, 10_000_000)
    if len(bars) < WARMUP + HORIZON + 2:
        return
    for i in range(WARMUP, len(bars) - HORIZON - 1):
        CUT["ts"] = bars[i].close_ts
        close = bars[i].ohlc.c
        a = _atr15(bars, i)
        try:
            votes = collect_votes(symbol, TF)
            dd = decide_direction(symbol, TF)
        except Exception:
            continue
        fwd = _forward(bars, i, close, a)
        if fwd is None:
            continue
        row = {
            "symbol": symbol,
            "ts": bars[i].close_ts,
            "close": round(close, 4),
            "atr": round(a, 4),
            "side": dd.side,
            "score": round(dd.score, 4),
            "bias": dd.bias_strength,
            # per-vote: signed contribution (direction*strength) keyed by module
            "votes": {v.module: round(v.direction * v.strength, 4) for v in votes},
            "vote_dirs": {v.module: (1 if v.direction > 0 else -1 if v.direction < 0 else 0) for v in votes},
            **_level_ctx(symbol, close, a),
            **fwd,
        }
        rows.append(row)


def main():
    print(f"=== bar availability ({TF}) ===")
    for s in SYMBOLS:
        print(f"  {s}: {len(_orig(s, TF, 10_000_000))} bars")
    rows = []
    for s in SYMBOLS:
        CUT["ts"] = None
        build_symbol(s, rows)
    CUT["ts"] = None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    # quick sanity: base rate + vote frequency
    up = sum(1 for r in rows if r["fwd_up"])
    print(f"\nwrote {len(rows)} rows → {OUT}")
    if rows:
        print(f"base rate P(up over {HORIZON} bars) = {100*up/len(rows):.1f}%")
        from collections import Counter
        vc = Counter()
        for r in rows:
            vc.update(r["votes"].keys())
        print("vote frequency:", dict(vc.most_common()))


if __name__ == "__main__":
    main()
