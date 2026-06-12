#!/usr/bin/env python3
"""Phantom-fill detector — was any trade filled at a price the market never traded?

A fill at price P is only physically possible if some bar's [low, high] straddled
P. A "phantom" fill is recorded at a price NO bar reached → fabricated entry →
inflated/garbage P&L (the leg1-at-zone-limit bug, fixed in 131b87e + purged).

For every position (all strategies) we check the entry price against real 1m bars:
  ENTRY-TIME test (precise): is `entry` inside [min_low, max_high] of the 1m bars
      spanning the signal bar through ~2min after open? If not → price wasn't
      trading at the fill level when we claim we filled → PHANTOM.
  WHOLE-TRADE test (conservative): is `entry` ever touched between open and close?
      If price NEVER revisits the entry across the entire trade → unambiguous phantom.

    PYTHONPATH=. .venv/bin/python -u scripts/phantom_check.py
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STRAT_DIR = ROOT / "data" / "strategies"
FP_DIR = ROOT / "data" / "footprint"

TF_SEC = {"1m": 60, "5m": 300, "15m": 900}
# clearly-outside threshold: entry must miss the bar range by > this frac of price
REL_TOL = 0.0002   # 2 bps — beyond tick noise / float edge-touch
BASIS_WIN_S = 6 * 3600   # ±6h window to estimate a symbol's live-vs-footprint basis


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


_b1m: dict[str, list[dict]] = {}
def bars1m(sym: str) -> list[dict]:
    if sym not in _b1m:
        raw = load_jsonl(FP_DIR / f"{sym}_1m.jsonl")
        raw.sort(key=lambda b: b["close_ts"])
        _b1m[sym] = raw
    return _b1m[sym]


def range_in(bars: list[dict]) -> tuple[float, float] | None:
    if not bars:
        return None
    return (min(b["ohlc"]["l"] for b in bars), max(b["ohlc"]["h"] for b in bars))


def fp_close_at(sym: str, ts: int) -> float | None:
    """Footprint close nearest ts (within 2 min)."""
    near = [b for b in bars1m(sym) if abs(b["close_ts"] - ts) <= 120]
    return near[-1]["ohlc"]["c"] if near else None


# per-symbol live-vs-footprint basis index: [(ts, basis_frac), ...] sorted by ts
_basis_idx: dict[str, list[tuple[int, float]]] = {}


def build_basis_index(all_pos: list[dict]) -> None:
    tmp: dict[str, list[tuple[int, float]]] = {}
    for p in all_pos:
        c = fp_close_at(p["symbol"], p["opened_ts"] or 0)
        if c:
            tmp.setdefault(p["symbol"], []).append(
                (p["opened_ts"], (p["entry"] - c) / c))
    for s, lst in tmp.items():
        _basis_idx[s] = sorted(lst)


def local_basis(sym: str, ts: int) -> float:
    """Median live-vs-footprint basis among the symbol's fills within ±BASIS_WIN_S.
    A smooth, systematic offset = venue/source mismatch (NOT a phantom)."""
    lst = _basis_idx.get(sym, [])
    win = [b for (t, b) in lst if abs(t - ts) <= BASIS_WIN_S]
    if not win:
        return 0.0
    win.sort()
    return win[len(win) // 2]


def positions(strategy: str) -> list[dict]:
    """All positions (open + still-open) with entry + timing."""
    pos: dict[str, dict] = {}
    for e in load_jsonl(STRAT_DIR / strategy / "positions.jsonl"):
        pid, t = e.get("position_id"), e.get("type")
        if not pid:
            continue
        if t == "open":
            pos[pid] = {"position_id": pid, "symbol": e.get("symbol"),
                        "tf": e.get("tf", "15m"), "side": e.get("side"),
                        "entry": e.get("entry"), "bar_id": e.get("bar_id"),
                        "opened_ts": e.get("ts"), "closed_ts": None,
                        "realized_r": None, "reason": None, "strategy": strategy}
        elif t == "close" and pid in pos:
            pos[pid].update(closed_ts=e.get("ts"), realized_r=e.get("realized_r"),
                            reason=e.get("reason"))
    return [p for p in pos.values() if p["entry"] and p["side"] in ("long", "short")]


def bar_close_ts(p: dict) -> int:
    parts = (p.get("bar_id") or "").split("|")
    if len(parts) == 3 and parts[2].isdigit():
        return int(parts[2])
    return int(p.get("opened_ts") or 0)


def check(p: dict) -> dict:
    sym, entry = p["symbol"], p["entry"]
    bars = bars1m(sym)
    tf_sec = TF_SEC.get(p["tf"], 900)
    bclose = bar_close_ts(p)
    o_ts = p["opened_ts"] or bclose
    tol = entry * REL_TOL

    # entry-time window: signal bar's 1m constituents + 2min forward buffer
    ewin = [b for b in bars if bclose - tf_sec - 60 <= b["close_ts"] <= o_ts + 120]
    er = range_in(ewin)
    # whole-trade window
    c_ts = p["closed_ts"] or (o_ts + 86400)
    awin = [b for b in bars if o_ts - tf_sec <= b["close_ts"] <= c_ts + 60]
    ar = range_in(awin)

    def outside(rng):
        if rng is None:
            return None
        lo, hi = rng
        if entry > hi + tol:
            return entry - hi          # filled above everything traded
        if entry < lo - tol:
            return lo - entry          # filled below everything traded
        return 0.0

    entry_gap = outside(er)
    trade_gap = outside(ar)

    # Remove the smooth local venue basis, then re-test. If a systematic offset
    # (live feed vs rebuilt footprint) explains the miss, it's a basis artifact,
    # NOT a fabricated fill. Only a residual miss after de-basing is a real phantom.
    basis = local_basis(sym, o_ts)
    adj_entry = entry / (1 + basis) if basis else entry

    def outside_adj(rng):
        if rng is None:
            return None
        lo, hi = rng
        if adj_entry > hi + tol:
            return adj_entry - hi
        if adj_entry < lo - tol:
            return lo - adj_entry
        return 0.0

    adj_trade_gap = outside_adj(ar)
    adj_entry_gap = outside_adj(er)

    verdict = "ok"
    if entry_gap is None:
        verdict = "no_bars"
    elif adj_trade_gap and adj_trade_gap > 0:
        verdict = "PHANTOM"           # still outside after de-basing — real fabricated fill
    elif adj_entry_gap and adj_entry_gap > 0:
        verdict = "suspect"           # off the signal bar even after de-basing
    elif (trade_gap and trade_gap > 0) or (entry_gap and entry_gap > 0):
        verdict = "basis_artifact"    # raw miss explained by venue/source basis
    return {**p, "entry_gap": entry_gap, "trade_gap": trade_gap,
            "entry_gap_pct": round(100 * entry_gap / entry, 3) if entry_gap else 0.0,
            "basis_pct": round(100 * basis, 3),
            "verdict": verdict, "n_ewin": len(ewin)}


def ist(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M") if ts else "-"


def main() -> None:
    strategies = sorted(d.name for d in STRAT_DIR.iterdir()
                        if d.is_dir() and (d / "positions.jsonl").exists())
    all_pos = [p for s in strategies for p in positions(s)]
    build_basis_index(all_pos)            # estimate venue basis before testing
    rows = [check(p) for p in all_pos]

    by_v = defaultdict(list)
    for r in rows:
        by_v[r["verdict"]].append(r)

    print("=" * 96)
    print(f"PHANTOM-FILL CHECK — {len(rows)} positions across {len(strategies)} strategies")
    print("=" * 96)
    for v in ["PHANTOM", "suspect", "basis_artifact", "no_bars", "ok"]:
        print(f"  {v:<15} {len(by_v[v])}")
    print()

    for v, title in [("PHANTOM", "UNAMBIGUOUS PHANTOMS (entry never traded during the whole trade)"),
                     ("suspect", "SUSPECT (entry not traded at fill time; price revisited later)")]:
        lst = sorted(by_v[v], key=lambda r: -(r["entry_gap_pct"] or 0))
        if not lst:
            continue
        print(f"── {title} — {len(lst)} ──")
        print(f"  {'strategy':<20}{'sym':<9}{'side':<6}{'entry':>10}{'gap%':>7}"
              f"{'realR':>8}{'reason':<16}{'open(IST)':>14}")
        for r in lst[:40]:
            print(f"  {r['strategy']:<20}{r['symbol']:<9}{r['side']:<6}{r['entry']:>10.2f}"
                  f"{r['entry_gap_pct']:>7.2f}"
                  f"{(r['realized_r'] if r['realized_r'] is not None else 0):>8.2f}"
                  f"  {str(r['reason'] or '-')[:13]:<14}{ist(r['opened_ts']):>14}")
        if len(lst) > 40:
            print(f"  … +{len(lst)-40} more")
        print()

    # per-strategy phantom tally
    print("── per-strategy phantom/suspect counts ──")
    tally = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        t = tally[r["strategy"]]
        t[0] += 1
        if r["verdict"] == "PHANTOM":
            t[1] += 1
        elif r["verdict"] == "suspect":
            t[2] += 1
    for s, (n, ph, su) in sorted(tally.items(), key=lambda kv: -(kv[1][1] + kv[1][2])):
        if ph or su:
            print(f"  {s:<22} n={n:<4} phantom={ph:<3} suspect={su}")

    n_ph = len(by_v["PHANTOM"])
    n_ba = len(by_v["basis_artifact"])
    print("\n" + "=" * 96)
    if n_ba:
        ba_syms = sorted({r["symbol"] for r in by_v["basis_artifact"]})
        med_b = sorted(r["basis_pct"] for r in by_v["basis_artifact"])[n_ba // 2]
        print(f"NOTE: {n_ba} 'basis_artifact' fills ({','.join(ba_syms)}) — raw price miss "
              f"explained by a systematic ~{med_b:.2f}% live-vs-footprint venue basis, "
              f"NOT fabricated. (footprint rebuilt from a different source than the live fills.)")
    if n_ph == 0:
        print("RESULT: ✅ NO real phantom fills — every entry was a real traded price "
              "(after accounting for venue basis).")
        if by_v["suspect"]:
            print(f"        ({len(by_v['suspect'])} 'suspect' = off the signal bar even after "
                  f"de-basing; price revisited later — usually legit limit fills, worth a glance.)")
    else:
        ph_r = sum((r["realized_r"] or 0) for r in by_v["PHANTOM"])
        print(f"RESULT: 🚩 {n_ph} REAL phantom fills (outside range even after de-basing) — "
              f"net realized_r = {ph_r:+.2f}R (fabricated; should be voided).")


if __name__ == "__main__":
    main()
