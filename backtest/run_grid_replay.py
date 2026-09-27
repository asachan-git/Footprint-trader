#!/usr/bin/env python3
"""Replay the 15m hvn_inside_touch grid over data/footprint/XAUTUSDT_{15m,5m}.jsonl.

    python backtest/run_grid_replay.py                 # baseline vs proposed + ablations
    python backtest/run_grid_replay.py --from 2026-05-07 --to 2026-08-01

Writes backtest/results/grid_replay.json and prints a table. Money is USD at
base_lot 0.01 on a standard (100 oz) contract, i.e. $1 per point for leg 1.
"""
from __future__ import annotations
import argparse, ast, json, pickle, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backtest.grid_replay import run, summarize, precompute  # noqa: E402

FP = ROOT / "data" / "footprint"
OUT = ROOT / "backtest" / "results"

PROPOSED = dict(cancel_opp_on_full=True, disaster_pts=140)
SCENARIOS = {
    "baseline (pre-server-tp)": {},
    "proposed (fullfill + orphan close + disaster cap)": PROPOSED,
    "proposed − disaster cap": dict(cancel_opp_on_full=True),
    "proposed, fullfill WITHOUT orphan close": {**PROPOSED, "close_opp_filled": False},
    "proposed + news clock block": {**PROPOSED, "news": True},
    "proposed + trapped@4": {**PROPOSED, "trapped_k": 4},
    "proposed + contested decay 0.9": {**PROPOSED, "contested_decay": 0.9},
    "proposed + opposite cap 2": {**PROPOSED, "opp_cap": 2},
    "proposed + BB size tilt": {**PROPOSED, "bb_tilt": True},
    "proposed, no leg TP": {**PROPOSED, "leg_tp": False},
    "proposed, 2x costs": {**PROPOSED, "spread": 0.5, "comm_per_lot": 12.0},
    "proposed, 3x costs": {**PROPOSED, "spread": 0.75, "comm_per_lot": 18.0},
    "proposed, path H->L": {**PROPOSED, "path": "hl"},
    "proposed, path L->H": {**PROPOSED, "path": "lh"},
}


def load(tf: str) -> pd.DataFrame:
    rows = []
    for line in open(FP / f"XAUTUSDT_{tf}.jsonl"):
        if not line.strip():
            continue
        d = json.loads(line)
        o = d["ohlc"]; o = ast.literal_eval(o) if isinstance(o, str) else o
        bl = d.get("bid_ladder") or []; al = d.get("ask_ladder") or []
        bl = ast.literal_eval(bl) if isinstance(bl, str) else bl
        al = ast.literal_eval(al) if isinstance(al, str) else al
        rows.append(dict(ts=int(float(d["close_ts"])), o=float(o["o"]), h=float(o["h"]), l=float(o["l"]),
                         c=float(o["c"]), bid=[(float(x["price"]), float(x["vol"])) for x in bl],
                         ask=[(float(x["price"]), float(x["vol"])) for x in al]))
    return pd.DataFrame(rows).drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t0"); ap.add_argument("--to", dest="t1")
    a = ap.parse_args()
    T = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp()) if s else None
    OUT.mkdir(parents=True, exist_ok=True)
    b15, b5 = load("15m"), load("5m")
    cache = OUT / "pre15.pkl"
    pre = pickle.load(open(cache, "rb")) if cache.exists() else precompute(b15)
    pickle.dump(pre, open(cache, "wb"))
    res = {}
    for name, cfg in SCENARIOS.items():
        cyc, eq = run(b15, b5, pre, cfg, T(a.t0), T(a.t1))
        s, df = summarize(cyc, eq)
        df["m"] = pd.to_datetime(df.ts, unit="s").dt.to_period("M").astype(str)
        s["months"] = df[df.filled > 0].groupby("m").pnl.sum().round(1).to_dict()
        res[name] = s
        print(f"{name:52s} net {s['net']:9.1f}  PF {s['pf']:5.2f}  win {s['win']:5.1f}%  "
              f"worst {s['worst']:8.1f}  maxDD {s['max_dd']:8.1f}  cycles {s['cycles']}")
    json.dump(res, open(OUT / "grid_replay.json", "w"), indent=1, default=float)


if __name__ == "__main__":
    main()
