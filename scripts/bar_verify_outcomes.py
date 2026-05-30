#!/usr/bin/env python3
"""Bar-based outcome verification and positions.jsonl / cycles.jsonl patch.

For every closed position:
  1. Reconstruct SL/TP trail from sl_adjust / tp_adjust / add_leg events
  2. Walk 1m bars between opened_ts and closed_ts (+ 5m buffer)
  3. Determine first bar where price crossed SL or TP
  4. Recompute realized_r using disaster-floor denominator (fixed, not trailing)
  5. Patch positions.jsonl close event and cycles.jsonl close event in-place

Disaster floor (matches grid_placer.py):
  BTCUSDT  : 3% of entry
  XAUTUSDT : 1.5% of entry

Output:
  - Console report: original vs bar-verified R per position
  - Writes patched JSONL files (atomic tmp → replace)
  - Adds field bar_verified=True to patched close events
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSITIONS_FILE = ROOT / "data" / "positions.jsonl"
CYCLES_FILE    = ROOT / "data" / "cycles.jsonl"
FP_DIR         = ROOT / "data" / "footprint"

FLOOR_PCT: dict[str, float] = {"BTCUSDT": 0.03, "XAUTUSDT": 0.015}

def disaster_risk(symbol: str, entry: float) -> float:
    return entry * FLOOR_PCT.get(symbol, 0.02)


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.open() if l.strip()]


def load_bars(symbol: str) -> list[dict]:
    p = FP_DIR / f"{symbol}_1m.jsonl"
    bars = load_jsonl(p)
    bars.sort(key=lambda b: b["close_ts"])
    return bars


# ── Reconstruct SL/TP trail for a position ──────────────────────────────────

def build_sl_tp_trail(
    pid: str,
    initial_sl: float,
    initial_tp: float,
    events: list[dict],   # ALL position events sorted by ts
) -> list[tuple[int, float, float]]:
    """Return list of (ts, sl, tp) — sorted by ts.

    Starting state at open, updated by sl_adjust / tp_adjust / add_leg events.
    """
    trail: list[tuple[int, float, float]] = []
    sl, tp = initial_sl, initial_tp
    for ev in events:
        if ev.get("position_id") != pid:
            continue
        t = ev["ts"]
        if ev["type"] == "sl_adjust":
            sl = float(ev["new_sl"])
        elif ev["type"] == "tp_adjust":
            tp = float(ev["new_tp"])
        elif ev["type"] == "add_leg":
            # add_leg may carry new_sl / new_tp
            if "new_sl" in ev:
                sl = float(ev["new_sl"])
            if "new_tp" in ev:
                tp = float(ev["new_tp"])
        trail.append((t, sl, tp))
    return trail


def sl_tp_at(ts: int, trail: list[tuple[int, float, float]],
             initial_sl: float, initial_tp: float) -> tuple[float, float]:
    """Return (sl, tp) active at time ts — last trail entry before ts."""
    sl, tp = initial_sl, initial_tp
    for (t, s, p) in trail:
        if t <= ts:
            sl, tp = s, p
        else:
            break
    return sl, tp


# ── Walk bars to find first SL/TP touch ─────────────────────────────────────

def walk_bars(
    bars: list[dict],
    open_ts: int,
    initial_sl: float,
    initial_tp: float,
    side: str,           # "long" | "short"
    trail: list[tuple[int, float, float]],
    buffer_s: int = 300,
) -> dict:
    """Walk 1m bars from open_ts.

    Returns {
        hit: "sl" | "tp" | "open" (still open at last bar),
        exit_price: float,
        exit_ts: int,
        exit_bar_high: float,
        exit_bar_low: float,
    }
    """
    window = [b for b in bars if open_ts <= b["close_ts"] <= open_ts + 24 * 3600 + buffer_s]

    for bar in window:
        bar_ts  = bar["close_ts"]
        bar_h   = bar["ohlc"]["h"]
        bar_l   = bar["ohlc"]["l"]
        sl, tp  = sl_tp_at(bar_ts, trail, initial_sl, initial_tp)

        if side == "long":
            # SL touched if bar low ≤ SL
            if bar_l <= sl:
                return {"hit": "sl", "exit_price": sl, "exit_ts": bar_ts,
                        "bar_h": bar_h, "bar_l": bar_l}
            # TP touched if bar high ≥ TP
            if bar_h >= tp:
                return {"hit": "tp", "exit_price": tp, "exit_ts": bar_ts,
                        "bar_h": bar_h, "bar_l": bar_l}
        else:  # short
            # SL touched if bar high ≥ SL
            if bar_h >= sl:
                return {"hit": "sl", "exit_price": sl, "exit_ts": bar_ts,
                        "bar_h": bar_h, "bar_l": bar_l}
            # TP touched if bar low ≤ TP
            if bar_l <= tp:
                return {"hit": "tp", "exit_price": tp, "exit_ts": bar_ts,
                        "bar_h": bar_h, "bar_l": bar_l}

    # Not hit within window — use last bar close
    if window:
        last = window[-1]
        return {"hit": "open", "exit_price": last["ohlc"]["c"],
                "exit_ts": last["close_ts"], "bar_h": last["ohlc"]["h"],
                "bar_l": last["ohlc"]["l"]}
    return {"hit": "open", "exit_price": initial_tp, "exit_ts": open_ts,
            "bar_h": 0.0, "bar_l": 0.0}


# ── Patch JSONL in-place ─────────────────────────────────────────────────────

def patch_jsonl(path: Path, patches: dict[str, dict]) -> int:
    """Overwrite close events matching patches[position_id | cycle_id].

    patches: {id_value: {fields_to_merge}}
    Returns count of patched lines.
    """
    lines = load_jsonl(path)
    patched = 0
    out = []
    for ev in lines:
        pid = ev.get("position_id") or ev.get("cycle_id")
        if ev.get("type") == "close" and pid in patches:
            ev.update(patches[pid])
            patched += 1
        out.append(ev)

    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for ev in out:
            fh.write(json.dumps(ev) + "\n")
    shutil.move(str(tmp), str(path))
    return patched


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    all_events = load_jsonl(POSITIONS_FILE)
    cycles     = load_jsonl(CYCLES_FILE)

    # Index
    pos_open:  dict[str, dict] = {}
    pos_close: dict[str, dict] = {}
    for ev in all_events:
        t = ev.get("type")
        pid = ev.get("position_id", "")
        if t == "open":
            pos_open[pid] = ev
        elif t == "close":
            pos_close[pid] = ev

    cy_open:  dict[str, str] = {}   # position_id → cycle_id
    cy_close: dict[str, dict] = {}  # cycle_id → close event
    for ev in cycles:
        t = ev.get("type")
        if t == "open" and ev.get("position_id"):
            cy_open[ev["position_id"]] = ev["cycle_id"]
        elif t == "close":
            cy_close[ev["cycle_id"]] = ev

    # Sort all events by ts for trail reconstruction
    sorted_events = sorted(all_events, key=lambda e: e.get("ts", 0))

    # Cache bars per symbol
    bars_cache: dict[str, list[dict]] = {}

    pos_patches:   dict[str, dict] = {}   # position_id → patch dict
    cycle_patches: dict[str, dict] = {}   # cycle_id → patch dict

    rows = []

    for pid, po in pos_open.items():
        pc = pos_close.get(pid)
        if not pc:
            continue  # still open

        sym    = po["symbol"]
        side   = po["side"]
        entry  = float(po["entry"])
        sl0    = float(po.get("stop_loss") or po.get("sl") or 0)
        tp0    = float(po.get("take_profit") or po.get("tp") or 0)
        open_ts = int(po["ts"])

        if sl0 <= 0 or tp0 <= 0:
            continue  # missing SL/TP — skip

        risk = disaster_risk(sym, entry)
        if risk <= 0:
            continue

        if sym not in bars_cache:
            bars_cache[sym] = load_bars(sym)
        bars = bars_cache[sym]

        # Build SL/TP trail from adjustment events
        trail = build_sl_tp_trail(pid, sl0, tp0, sorted_events)

        # Walk bars
        result = walk_bars(bars, open_ts, sl0, tp0, side, trail)

        hit        = result["hit"]
        exit_price = result["exit_price"]
        exit_ts    = result["exit_ts"]

        # Recompute R in disaster-floor units
        if side == "long":
            bar_r = (exit_price - entry) / risk
        else:
            bar_r = (entry - exit_price) / risk

        # Original stored R
        stored_r = float(pc.get("realized_r", 0))
        orig_reason = pc.get("reason", "")

        # Build patch
        new_reason = f"bar_verified:{hit} @ {exit_price:.2f}"
        pos_patch = {
            "realized_r":   round(bar_r, 6),
            "bar_verified": True,
            "bar_hit":      hit,
            "bar_exit_ts":  exit_ts,
            "bar_exit_price": round(exit_price, 4),
            "orig_realized_r": stored_r,
            "orig_reason":  orig_reason,
        }
        pos_patches[pid] = pos_patch

        cid = cy_open.get(pid)
        if cid:
            cy_patch = {
                "realized_pnl": round(bar_r, 6),
                "bar_verified": True,
                "bar_hit":      hit,
                "orig_realized_pnl": cy_close.get(cid, {}).get("realized_pnl"),
            }
            cycle_patches[cid] = cy_patch

        rows.append({
            "pid":        pid,
            "sym":        sym,
            "side":       side,
            "entry":      entry,
            "sl0":        sl0,
            "tp0":        tp0,
            "stored_r":   stored_r,
            "bar_r":      bar_r,
            "hit":        hit,
            "exit_price": exit_price,
            "was_sentinel": (stored_r == -1.0),
        })

    # ── Report ───────────────────────────────────────────────────────────────
    print("=" * 70)
    print("BAR-VERIFIED OUTCOME AUDIT")
    print("=" * 70)
    print(f"Closed positions processed : {len(rows)}")
    print()

    tp_rows = [r for r in rows if r["hit"] == "tp"]
    sl_rows = [r for r in rows if r["hit"] == "sl"]
    open_rows = [r for r in rows if r["hit"] == "open"]
    sent_rows = [r for r in rows if r["was_sentinel"]]

    print(f"Bar-detected TP hits   : {len(tp_rows)}")
    print(f"Bar-detected SL hits   : {len(sl_rows)}")
    print(f"No hit in bar window   : {len(open_rows)}")
    print(f"Were sentinel (-1.0)   : {len(sent_rows)}")
    print()

    import statistics as _s

    all_bar_r  = [r["bar_r"]  for r in rows]
    all_orig_r = [r["stored_r"] for r in rows]

    print(f"{'Metric':<22} {'Stored R':>10} {'Bar R':>10} {'Delta':>10}")
    print("-" * 54)
    print(f"{'Total R':<22} {sum(all_orig_r):>+10.3f} {sum(all_bar_r):>+10.3f} {sum(all_bar_r)-sum(all_orig_r):>+10.3f}")
    print(f"{'Avg R/trade':<22} {_s.mean(all_orig_r):>+10.3f} {_s.mean(all_bar_r):>+10.3f} {_s.mean(all_bar_r)-_s.mean(all_orig_r):>+10.3f}")
    print(f"{'Median R':<22} {_s.median(all_orig_r):>+10.3f} {_s.median(all_bar_r):>+10.3f}")
    print()

    wins_bar  = sum(1 for r in rows if r["bar_r"] > 0)
    wins_orig = sum(1 for r in rows if r["stored_r"] > 0)
    n = len(rows)
    print(f"WR stored : {wins_orig}/{n} = {wins_orig/n*100:.0f}%")
    print(f"WR bar    : {wins_bar}/{n}  = {wins_bar/n*100:.0f}%")
    print()

    print("Per symbol:")
    for sym in ["BTCUSDT", "XAUTUSDT"]:
        rs = [r for r in rows if r["sym"] == sym]
        if not rs: continue
        orig = sum(r["stored_r"] for r in rs)
        bar  = sum(r["bar_r"]    for r in rs)
        w_b  = sum(1 for r in rs if r["bar_r"] > 0)
        print(f"  {sym}: n={len(rs):3d}  stored totalR={orig:+.3f}  bar totalR={bar:+.3f}  bar WR={w_b/len(rs)*100:.0f}%")
    print()

    print("Mismatches (stored vs bar outcome differ):")
    mismatch_count = 0
    for r in rows:
        s_hit = "tp" if r["stored_r"] > 0.05 else ("sl" if r["stored_r"] < -0.01 else "scratch")
        b_hit = r["hit"]
        # Only flag meaningful flips
        if s_hit == "tp" and b_hit == "sl":
            print(f"  {r['pid'][:8]} {r['sym']:12s} {r['side']:5s}  STORED=TP({r['stored_r']:+.3f}R) BAR=SL({r['bar_r']:+.3f}R)")
            mismatch_count += 1
        elif s_hit == "sl" and b_hit == "tp":
            print(f"  {r['pid'][:8]} {r['sym']:12s} {r['side']:5s}  STORED=SL({r['stored_r']:+.3f}R) BAR=TP({r['bar_r']:+.3f}R)")
            mismatch_count += 1
    if mismatch_count == 0:
        print("  None — stored outcomes match bar walk.")
    print()

    # ── Patch files ──────────────────────────────────────────────────────────
    print("Patching positions.jsonl ...")
    n_pos = patch_jsonl(POSITIONS_FILE, pos_patches)
    print(f"  Patched {n_pos} close events")

    print("Patching cycles.jsonl ...")
    n_cy = patch_jsonl(CYCLES_FILE, cycle_patches)
    print(f"  Patched {n_cy} close events")

    print()
    print("Done. Restart server (or reload position_store) to reflect on dashboard.")


if __name__ == "__main__":
    main()
