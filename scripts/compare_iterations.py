"""Compare two observation snapshots side-by-side.

Usage:
  python3 scripts/compare_iterations.py                  # latest two
  python3 scripts/compare_iterations.py --prev iter_X --curr iter_Y
  python3 scripts/compare_iterations.py --list           # list all snapshots
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OBS_DIR = ROOT / "data" / "observations"
IST = timezone(timedelta(hours=5, minutes=30))


def _ist(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d %H:%M IST")


def _list_snapshots() -> list[Path]:
    if not OBS_DIR.exists():
        return []
    return sorted(OBS_DIR.glob("iter_*.json"))


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def _diff_dec(old: dict, new: dict, sym: str) -> str:
    o = old.get(sym, {})
    n = new.get(sym, {})
    parts = []
    for k in ("long", "short", "flat", "rejected"):
        dl = n.get(k, 0) - o.get(k, 0)
        if dl != 0:
            sign = "+" if dl > 0 else ""
            parts.append(f"{k} {sign}{dl}")
    return ", ".join(parts) if parts else "no change"


def _diff_votes(old: dict, new: dict) -> list[str]:
    lines = []
    modules = sorted(set(old) | set(new))
    for m in modules:
        o = old.get(m, {})
        n = new.get(m, {})
        deltas = []
        for k in ("long", "short", "abstain"):
            d = n.get(k, 0) - o.get(k, 0)
            if d != 0:
                sign = "+" if d > 0 else ""
                deltas.append(f"{k}{sign}{d}")
        ok = sum(n.get(k, 0) for k in ("long", "short", "abstain"))
        if not deltas:
            continue
        # health flag
        if ok > 0 and n.get("long", 0) == 0 and n.get("short", 0) > 0:
            flag = " ⚠ short-only"
        elif ok > 0 and n.get("short", 0) == 0 and n.get("long", 0) > 0:
            flag = " ⚠ long-only"
        elif ok == 0:
            flag = " ⚠ silent"
        else:
            flag = ""
        lines.append(f"  {m:14} {', '.join(deltas):<28}{flag}")
    return lines


def report(old: dict, new: dict) -> str:
    out = []
    out.append(f"# Iteration compare: {_ist(old['iter_ts'])}  →  {_ist(new['iter_ts'])}")
    out.append(f"git: {old.get('git_sha')}  →  {new.get('git_sha')}")
    out.append("")

    # Settings diff
    so = old.get("settings_excerpt", {})
    sn = new.get("settings_excerpt", {})
    out.append("## Settings diff")
    for k in sorted(set(so) | set(sn)):
        if so.get(k) != sn.get(k):
            out.append(f"  {k}: {so.get(k)}  →  {sn.get(k)}")
    out.append("")

    # Decisions per symbol
    do = old.get("decisions_last_24h", {})
    dn = new.get("decisions_last_24h", {})
    out.append("## Decisions per symbol (24h)")
    for sym in sorted(set(do) | set(dn)):
        out.append(f"  {sym}: {_diff_dec(do, dn, sym)}")
    out.append("")

    # Cycle outcomes
    co = old.get("cycles_outcome_last_24h", {})
    cn = new.get("cycles_outcome_last_24h", {})
    out.append("## Cycle outcomes (24h)")
    for k in ("total_cycles", "tp_hit", "sl_hit", "invalidated", "still_open",
              "max_legs_used", "sum_R", "median_R"):
        ov, nv = co.get(k), cn.get(k)
        if ov is None and nv is None:
            continue
        delta = ""
        if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            d = nv - ov
            delta = f"  ({'+' if d >= 0 else ''}{d:.2f})" if isinstance(d, float) else f"  ({'+' if d >= 0 else ''}{d})"
        out.append(f"  {k:18} {ov} → {nv}{delta}")
    out.append("")

    # Mode 2 vote distribution drift
    vo = old.get("mode2_vote_distribution_last_24h", {})
    vn = new.get("mode2_vote_distribution_last_24h", {})
    out.append("## Mode 2 vote distribution drift")
    drift = _diff_votes(vo, vn)
    if drift:
        out.extend(drift)
    else:
        out.append("  (no vote module changed since last iter)")
    out.append("")

    # Health flags
    flags = []
    for m in ("vp_shape", "wave", "sweep"):
        v = vn.get(m, {})
        if sum(v.get(k, 0) for k in ("long", "short", "abstain")) == 0:
            flags.append(f"⚠ {m} still silent (0 votes in 24h)")
    if (vn.get("cvd", {}).get("long", 0) == 0 and
        vn.get("cvd", {}).get("short", 0) > 0):
        flags.append("⚠ cvd voting short-only")
    # Mode 2 longs appearing for first time?
    o_longs = sum(v.get("long", 0) for v in vo.values())
    n_longs = sum(v.get("long", 0) for v in vn.values())
    if o_longs == 0 and n_longs > 0:
        flags.append(f"✓ Mode 2 producing LONGs (was 0, now {n_longs})")
    if flags:
        out.append("## Health flags")
        for f in flags:
            out.append(f"  {f}")
        out.append("")

    # Open positions
    op = new.get("open_positions", [])
    if op:
        out.append("## Open positions (current snapshot)")
        for p in op:
            out.append(f"  {p['symbol']:10} {p['side']:5} {p['id']}: "
                       f"avg={p['avg_entry']} TP={p['tp']} SL={p['sl']} legs={p['legs_filled']}")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List all snapshots")
    ap.add_argument("--prev", help="Previous snapshot filename (without .json)")
    ap.add_argument("--curr", help="Current snapshot filename")
    args = ap.parse_args()

    snaps = _list_snapshots()
    if args.list:
        for s in snaps:
            print(s.name, _ist(json.loads(s.read_text()).get("iter_ts", 0)))
        return

    if len(snaps) < 2:
        print("Need at least 2 snapshots to compare. Run server twice or "
              "execute observation_logger manually:")
        print("  python3 -m execution.observation_logger manual")
        sys.exit(1)

    prev_path = OBS_DIR / f"{args.prev}.json" if args.prev else snaps[-2]
    curr_path = OBS_DIR / f"{args.curr}.json" if args.curr else snaps[-1]

    old = _load(prev_path)
    new = _load(curr_path)

    report_text = report(old, new)
    print(report_text)

    out_md = ROOT / "data" / f"iter_compare_{prev_path.stem}_vs_{curr_path.stem}.md"
    out_md.write_text(report_text)
    print(f"\nReport saved to {out_md}")


if __name__ == "__main__":
    main()
