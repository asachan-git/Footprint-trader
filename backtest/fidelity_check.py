"""Phase-1 acceptance test — does the harness reproduce REAL live arms?

Compares harness output against `data/cycles/cycle_outcomes_*.jsonl`, which is real
live ground truth (arm context + exit, already joined, one row per completed cycle).

Gates, in strict order — each is only meaningful if the previous passed:

  G1 arm recall     every live arm has a harness arm (same magic, fulcrum within tol)
  G2 no phantoms    harness arms with no live counterpart
  G3 geometry       step / n_per_side / buy_n / sell_n / skew match exactly
  G4 targets        tp_up / tp_down / node_low / node_high match

G1-G4 test the STRATEGY BRAIN, which is unmodified production code. A mismatch
means a seam is leaking (wrong VP snapshot, wrong clock, stale quote, wrong venue
offset) — not noise. Fill-side error (exit reason, P&L) is Phase 2's G5/G6.

Deliberate scope limits, reported not hidden:
  - candle_sweep is EXCLUDED: it requires real Vantage venue OHLC, which the repo
    has none of historically (venue capture only started 2026-07-21). Its rows are
    counted and reported as UNVALIDATED, never silently passed.
  - The harness's pending-only fill model means a cycle whose legs never fill can
    legitimately re-arm (fade/re-anchor policy). Harness arms are therefore deduped
    to the FIRST arm per (magic, rounded fulcrum) before comparison.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Kinds that run off store() bars and are fully backtestable today.
BACKTESTABLE = {"hvn_inside_touch", "lvn_edge_touch", "hvn_edge", "hvn_displacement"}
# Kinds blocked on missing venue history.
DEFERRED = {"candle_sweep"}


def load_live(day: str, broker_symbol: str = "XAUUSD+") -> list[dict]:
    p = ROOT / "data" / "cycles" / f"cycle_outcomes_{day}.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"no live ground truth for {day}: {p}")
    rows = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("broker_symbol") == broker_symbol:
            rows.append(r)
    return rows


def dedupe_harness_arms(cmds: list[dict], fulcrum_round: int = 1) -> list[dict]:
    """First PLACE_PENDING batch per (magic, rounded fulcrum-proxy).

    A grid arm emits one PLACE_PENDING per leg; we group them into one arm record
    carrying the leg prices, so geometry can be derived the same way live records it.
    """
    by_key: dict[tuple, dict] = {}
    for c in cmds:
        if c.get("type") != "PLACE_PENDING":
            continue
        mg = int(c.get("magic", 0) or 0)
        price = float(c.get("price") or 0.0)
        # Legs of ONE arm are emitted in the SAME poll. The per-leg `comment` tag
        # (FB|lvn_edge|3m|b1, |b2, ...) identifies the leg SLOT, not the arm — keying
        # on it merges every re-arm of that slot across the whole day.
        key = (mg, float(c.get("poll_ts") or 0.0))
        rec = by_key.setdefault(key, {"magic": mg, "poll_ts": key[1], "legs": []})
        rec["legs"].append({"price": price, "order_type": c.get("order_type"),
                            "lot": c.get("lot"), "tp": c.get("tp"), "sl": c.get("sl")})
    out = []
    for rec in by_key.values():
        legs = rec["legs"]
        buys = [l for l in legs if "buy" in str(l.get("order_type", "")).lower()]
        sells = [l for l in legs if "sell" in str(l.get("order_type", "")).lower()]
        prices = sorted(l["price"] for l in legs)
        rec["buy_n"] = len(buys)
        rec["sell_n"] = len(sells)
        rec["n_legs"] = len(legs)
        rec["price_lo"] = prices[0] if prices else None
        rec["price_hi"] = prices[-1] if prices else None
        # step = median gap between consecutive distinct leg prices
        gaps = [round(b - a, 4) for a, b in zip(prices, prices[1:]) if b > a]
        rec["step"] = round(sorted(gaps)[len(gaps) // 2], 4) if gaps else None
        tps = {round(float(l["tp"]), 4) for l in legs if l.get("tp")}
        rec["tps"] = sorted(tps)
        out.append(rec)
    return out


def run_gates(live_rows: list[dict], harness_arms: list[dict],
              fulcrum_tol: float = 0.5, ts_tol_s: float = 900.0) -> dict:
    live_bt = [r for r in live_rows if r.get("trigger_kind") in BACKTESTABLE]
    live_def = [r for r in live_rows if r.get("trigger_kind") in DEFERRED]
    live_other = [r for r in live_rows
                  if r.get("trigger_kind") not in BACKTESTABLE | DEFERRED]

    h_by_magic: dict[int, list[dict]] = defaultdict(list)
    for a in harness_arms:
        h_by_magic[a["magic"]].append(a)

    matched, missed = [], []
    for r in live_bt:
        mg = int(r.get("magic", 0) or 0)
        cands = h_by_magic.get(mg, [])
        if not cands:
            missed.append(r)
            continue
        # Match the harness arm CLOSEST IN TIME to the live arm. Taking cands[0]
        # compared every live cycle against one arbitrary harness arm on that magic,
        # which made geometry look 0% even where it was close.
        lt = float(r.get("armed_ts") or 0.0)
        best = min(cands, key=lambda a: abs(float(a.get("poll_ts") or 0.0) - lt))
        # A magic-only match is NOT a match: magics repeat all day, so pairing a live
        # 15:00 arm with a harness 00:20 arm on the same magic reports high recall for
        # two events that have nothing to do with each other. Require time proximity.
        if abs(float(best.get("poll_ts") or 0.0) - lt) > ts_tol_s:
            missed.append(r)
            continue
        matched.append((r, best))

    live_magics = {int(r.get("magic", 0) or 0) for r in live_bt}
    phantoms = [a for a in harness_arms if a["magic"] not in live_magics]

    g3_ok = g3_tot = 0
    g3_detail = []
    field_fail: dict[str, int] = defaultdict(int)
    step_ratios: list[float] = []
    for r, a in matched:
        g3_tot += 1
        checks = {
            "buy_n": (r.get("buy_n"), a.get("buy_n")),
            "sell_n": (r.get("sell_n"), a.get("sell_n")),
            "step": (r.get("step"), a.get("step")),
        }
        # step is a float derived from ATR — compare within 10% rather than exactly.
        # Leg counts must match exactly (they're integers straight off the planner).
        def _bad(k, v) -> bool:
            if v[0] is None or v[1] is None:
                return False
            if k == "step":
                lo, hi = float(v[0]), float(v[1])
                return abs(hi - lo) > 0.10 * max(abs(lo), 1e-9)
            return v[0] != v[1]

        bad = {k: v for k, v in checks.items() if _bad(k, v)}
        for k in bad:
            field_fail[k] += 1
        if r.get("step") and a.get("step"):
            step_ratios.append(float(a["step"]) / float(r["step"]))
        if not bad:
            g3_ok += 1
        else:
            g3_detail.append({"magic": r.get("magic"), "kind": r.get("trigger_kind"),
                              "mismatch": {k: {"live": v[0], "harness": v[1]}
                                           for k, v in bad.items()}})

    n_live = len(live_bt)
    # Arm-time coverage — surfaces the "harness armed at 00:20, live armed 09:43+"
    # failure mode that a magic-only match would otherwise hide behind high recall.
    h_ts = sorted(float(a.get("poll_ts") or 0.0) for a in harness_arms)
    l_ts = sorted(float(r.get("armed_ts") or 0.0) for r in live_bt)
    coverage = None
    if h_ts and l_ts:
        overlap = sum(1 for t in l_ts if h_ts[0] <= t <= h_ts[-1])
        coverage = {
            "harness_window": [int(h_ts[0]), int(h_ts[-1])],
            "live_window": [int(l_ts[0]), int(l_ts[-1])],
            "live_arms_in_harness_window": overlap,
            "live_arms_outside": len(l_ts) - overlap,
        }
    return {
        "arm_time_coverage": coverage,
        "day_rows_total": len(live_rows),
        "backtestable": n_live,
        "deferred_candle_sweep": len(live_def),
        "other_kinds": len(live_other),
        "harness_arms": len(harness_arms),
        "G1_arm_recall": {
            "matched": len(matched), "missed": len(missed),
            "pct": round(100.0 * len(matched) / n_live, 1) if n_live else None,
            "missed_kinds": sorted({r.get("trigger_kind") for r in missed}),
        },
        "G2_phantoms": {
            "count": len(phantoms),
            "pct_of_harness": round(100.0 * len(phantoms) / len(harness_arms), 1)
            if harness_arms else None,
            "magics": sorted({a["magic"] for a in phantoms})[:20],
        },
        "G3_geometry": {
            "exact": g3_ok, "compared": g3_tot,
            "pct": round(100.0 * g3_ok / g3_tot, 1) if g3_tot else None,
            "field_failures": dict(field_fail),
            "step_ratio_harness_over_live": {
                "median": round(sorted(step_ratios)[len(step_ratios) // 2], 3),
                "min": round(min(step_ratios), 3), "max": round(max(step_ratios), 3),
            } if step_ratios else None,
            "mismatches": g3_detail[:10],
        },
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--harness-cmds", required=True,
                    help="jsonl of commands from backtest.harness --out")
    ap.add_argument("--broker-symbol", default="XAUUSD+")
    args = ap.parse_args()

    live = load_live(args.day, args.broker_symbol)
    cmds = [json.loads(l) for l in Path(args.harness_cmds).read_text().splitlines() if l.strip()]
    arms = dedupe_harness_arms(cmds)
    rep = run_gates(live, arms)

    print(json.dumps(rep, indent=2))
    print()
    if rep["deferred_candle_sweep"]:
        print(f"NOTE: {rep['deferred_candle_sweep']} candle_sweep cycles EXCLUDED — "
              f"UNVALIDATED, needs real Vantage venue OHLC (capture started 2026-07-21).")
    g1 = rep["G1_arm_recall"]["pct"]
    g3 = rep["G3_geometry"]["pct"]
    print(f"VERDICT: G1={g1}%  G3={g3}%  "
          f"({'PASS' if (g1 or 0) >= 95 and (g3 or 0) >= 99 else 'FAIL — do not trust any P&L built on this'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
