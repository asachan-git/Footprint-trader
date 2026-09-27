#!/usr/bin/env python3
"""Demo vs replay: did the live bridge do what the replay says it should have?

Replays the same dates the demo ran, with the demo's own settings, and matches
cycles one-to-one (same setup, arm within ±15 min). Everything is converted to
POINTS AT BASE LOT so account currency and lot size cancel out.

    python backtest/compare_demo.py \
        --cycles data/cycles --report ~/Downloads/ReportHistory-<acct>.html \
        --from 2026-09-29 --to 2026-10-13

    --report is the MT5 "Report → History" HTML export. It gives realized P&L per
    cycle (incl. the half booked by the bias trail). Without it the script falls back
    to the cycle log's pnl_at_exit, which misses booked halves.

Writes backtest/results/compare_demo.json and prints a summary + the worst gaps.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backtest.grid_replay import run, precompute  # noqa: E402
from backtest.run_grid_replay import load as load_bars, OUT  # noqa: E402

KIND2STRAT = {"hvn_inside_touch": "hvn", "lvn_edge_touch": "lvn",
              "hvn_edge": "hvn_edge", "candle_sweep": "sweep"}
TAG2STRAT = {"hvn": "hvn", "lvn_edge": "lvn", "hvn_edge": "hvn_edge", "candle_s": "sweep"}
MATCH_S = 15 * 60
# replay exit names → the bridge's exit_reason names
REASON = {"disaster": "max_loss", "booked_out": "all_closed"}


# ── settings → replay config (native currency → $ at 0.01 base lot = points) ──────
def units(g: dict) -> float:
    """Native currency per point at base lot."""
    return (float(g.get("base_lot", 0.01)) * float(g.get("contract_size", 100.0))
            * float(g.get("account_ccy_per_usd", 1.0)))


def replay_cfg(settings: dict, tf: str = "15m") -> dict:
    g = settings.get("grid_levels") or {}
    u = units(g)
    base = float(g.get("base_lot", 0.01))
    tgt = (g.get("cycle_net_target_by_tf") or {}).get(tf, g.get("cycle_net_target_usd", 66.0))
    act = (g.get("bias_trail_activate_by_tf") or {}).get(tf, g.get("bias_trail_activate_usd", 6.67))
    return dict(
        base_lot=0.01,
        lot_step=0.01 * float(g.get("lot_step", 0.01)) / base,
        max_lots=0.01 * float(g.get("max_lots", 0.04)) / base,
        max_legs=int((g.get("hvn_max_legs_by_tf") or {}).get(tf, 5)),
        step_mult=float(g.get("mean_rev_step_mult", 0.5)),
        target=float(tgt) / u,
        min_target=float(g.get("cycle_min_target_usd", 0.33)) / u,
        decay=float(g.get("cycle_hedge_decay_pct", 33.0)) / 100.0,
        trail_act=float(act) / u,
        giveback=float(g.get("bias_trail_giveback_pct", 35.0)) / 100.0,
        book_frac=float(g.get("bias_book_frac", 0.5)),
        full_hedge=bool(g.get("cycle_close_on_full_hedge", True)),
        cancel_opp_on_full=bool(g.get("fullfill_be_enabled") and g.get("fullfill_cancel_opposite")),
        close_opp_filled=bool(g.get("fullfill_close_opposite_filled", True)),
        disaster_pts=float(g.get("cycle_max_loss_pts", 0.0) or 0.0),
    )


def enabled_strats(settings: dict, tf: str = "15m") -> list[str]:
    out = []
    for t in (settings.get("grid_levels") or {}).get("triggers") or []:
        k = t.get("kind")
        if k in KIND2STRAT and t.get("enabled", True) and tf in (t.get("tfs") or []):
            out.append(KIND2STRAT[k])
    return out


# ── demo side ─────────────────────────────────────────────────────────────────────
def load_cycles(cycles_dir: str, tf: str, t0: float, t1: float) -> pd.DataFrame:
    rows = []
    for f in sorted(glob.glob(str(Path(cycles_dir).expanduser() / "cycle_outcomes_*.jsonl"))):
        for line in open(f):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[(df.tf == tf) & df.trigger_kind.isin(KIND2STRAT) & (df.armed_ts >= t0) & (df.armed_ts < t1)]
    out = []
    for cid, g in df.groupby("cycle_id"):
        g = g.sort_values("exit_ts")
        final = g[~g.get("partial", pd.Series(False, index=g.index)).fillna(False).astype(bool)]
        last = final.iloc[-1] if len(final) else g.iloc[-1]
        out.append(dict(cycle_id=cid, strat=KIND2STRAT[last.trigger_kind], magic=int(last.magic),
                        armed_ts=float(last.armed_ts), exit_ts=float(last.exit_ts),
                        exit_reason=last.exit_reason, booked=bool((g.exit_reason == "bias_book_trail").any()),
                        filled=int(g.max_pos_seen.fillna(0).max()) if "max_pos_seen" in g else 1,
                        pnl_log=float(last.get("pnl_at_exit") or 0.0)))
    return pd.DataFrame(out)


def load_report(path: str, utc_offset_h: float) -> pd.DataFrame:
    """Positions table of an MT5 History report → one row per closed FB position (UTC epoch)."""
    raw = open(Path(path).expanduser(), "rb").read()
    text = raw.decode("utf-16", errors="ignore") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "ignore")
    df = pd.read_html(io.StringIO(text))[0]
    c0 = df.iloc[:, 0].astype(str)
    sec = {v: i for i, v in enumerate(c0) if v in ("Positions", "Orders")}
    P = df.iloc[sec["Positions"] + 2:sec["Orders"]]
    P = P[P[0].astype(str).str.match(r"\d{4}\.")]
    num = lambda s: pd.to_numeric(s.astype(str).str.replace("\xa0", "").str.replace(" ", ""), errors="coerce")
    ts = lambda s: ((pd.to_datetime(s, format="%Y.%m.%d %H:%M:%S") - pd.Timedelta(hours=utc_offset_h)
                     - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1))
    pos = pd.DataFrame({"open_ts": ts(P[0]), "comment": P[4].astype(str), "close_ts": ts(P[16]),
                        "net": num(P[18]).fillna(0) + num(P[19]).fillna(0) + num(P[20]).fillna(0)})
    m = pos.comment.str.extract(r"^FB\|([^|]+)\|([^|]+)\|")
    pos["strat"], pos["tf"] = m[0].map(TAG2STRAT), m[1]
    return pos.dropna(subset=["strat"])


def attach_realized(cyc: pd.DataFrame, pos: pd.DataFrame, tf: str) -> pd.DataFrame:
    cyc = cyc.copy()
    cyc["pnl_native"] = np.nan
    pos = pos[pos.tf == tf]
    for s, g in cyc.groupby("strat"):
        g = g.sort_values("armed_ts")
        p = pos[pos.strat == s]
        # a position belongs to the latest cycle of that setup armed before it opened
        idx = np.searchsorted(g.armed_ts.values, p.open_ts.values, side="right") - 1
        ok = idx >= 0
        sums = pd.Series(p.net.values[ok]).groupby(g.index.values[idx[ok]]).sum()
        cyc.loc[sums.index, "pnl_native"] = sums.values
    cyc["pnl_native"] = cyc.pnl_native.fillna(0.0)
    return cyc


# ── match + summarize ─────────────────────────────────────────────────────────────
def pf(x: pd.Series) -> float:
    gl = -x[x < 0].sum()
    return round(float(x[x > 0].sum() / gl), 2) if gl > 0 else float("inf")


def compare(demo: pd.DataFrame, rep: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    pairs, used = [], set()
    for s in sorted(set(demo.strat) | set(rep.strat)):
        d = demo[demo.strat == s].sort_values("armed_ts")
        r = rep[rep.strat == s].sort_values("arm_ts")
        for di, dr in d.iterrows():
            cand = r[(abs(r.arm_ts - dr.armed_ts) <= MATCH_S) & ~r.index.isin(list(used))]
            if len(cand):
                ri = (cand.arm_ts - dr.armed_ts).abs().idxmin(); used.add(ri)
                rr = r.loc[ri]
                pairs.append(dict(strat=s, demo_ts=dr.armed_ts, replay_ts=rr.arm_ts, demo_pts=dr.pts,
                                  replay_pts=rr.pnl, demo_exit=dr.exit_reason, replay_exit=rr.reason))
            else:
                pairs.append(dict(strat=s, demo_ts=dr.armed_ts, demo_pts=dr.pts, demo_exit=dr.exit_reason))
        for ri, rr in r[~r.index.isin(list(used))].iterrows():
            pairs.append(dict(strat=s, replay_ts=rr.arm_ts, replay_pts=rr.pnl, replay_exit=rr.reason))
    P = pd.DataFrame(pairs)
    summ = {}
    for s, g in P.groupby("strat"):
        m = g.dropna(subset=["demo_pts", "replay_pts"])
        dp, rp = g.demo_pts.dropna(), g.replay_pts.dropna()
        summ[s] = dict(
            demo_cycles=int(len(dp)), replay_cycles=int(len(rp)), matched=int(len(m)),
            match_rate=round(len(m) / max(1, len(dp)), 2),
            demo_net_pts=round(float(dp.sum()), 1), replay_net_pts=round(float(rp.sum()), 1),
            demo_pf=pf(dp), replay_pf=pf(rp),
            matched_demo_pts=round(float(m.demo_pts.sum()), 1), matched_replay_pts=round(float(m.replay_pts.sum()), 1),
            matched_corr=round(float(m.demo_pts.corr(m.replay_pts)), 2) if len(m) > 2 else None,
            median_abs_gap_pts=round(float((m.demo_pts - m.replay_pts).abs().median()), 1) if len(m) else None,
            same_exit_rate=round(float((m.demo_exit == m.replay_exit).mean()), 2) if len(m) else None,
            demo_only=int(g.replay_pts.isna().sum()), replay_only=int(g.demo_pts.isna().sum()),
        )
    return summ, P


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", default=str(ROOT / "data" / "cycles"))
    ap.add_argument("--report", help="MT5 History report HTML (recommended)")
    ap.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    ap.add_argument("--from", dest="t0", required=True)
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--tf", default="15m")
    a = ap.parse_args(argv)
    T = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp())
    t0, t1 = T(a.t0), T(a.t1)
    settings = yaml.safe_load(open(a.settings))
    g = settings.get("grid_levels") or {}
    u = units(g)
    cfg = replay_cfg(settings, a.tf)

    demo = load_cycles(a.cycles, a.tf, t0, t1)
    if demo.empty:
        sys.exit(f"no {a.tf} cycles in {a.cycles} between {a.t0} and {a.t1}")
    if a.report:
        off = float((settings.get("execution") or {}).get("broker_utc_offset_hours", 3))
        demo = attach_realized(demo, load_report(a.report, off), a.tf); src = "mt5_report"
    else:
        demo["pnl_native"] = demo.pnl_log; src = "cycle_log_pnl_at_exit (misses booked halves)"
    demo["pts"] = demo.pnl_native / u

    b15, b5 = load_bars("15m"), load_bars("5m")
    pre = precompute(b15)
    strats = sorted(set(demo.strat) | set(enabled_strats(settings, a.tf)))
    reps = []
    for s in strats:
        cyc, _ = run(b15, b5, pre, {**cfg, "strat": s}, t0, t1)
        reps += [dict(strat=s, arm_ts=c.arm_ts, pnl=c.realized, reason=REASON.get(c.exit_reason, c.exit_reason))
                 for c in cyc if any(l.filled for l in c.legs)]
    rep = pd.DataFrame(reps, columns=["strat", "arm_ts", "pnl", "reason"])
    # never-filled cycles: nothing to compare (the replay drops them the same way)
    demo = demo[demo.filled > 0]

    summ, P = compare(demo, rep)
    OUT.mkdir(parents=True, exist_ok=True)
    res = dict(window=[a.t0, a.t1], tf=a.tf, pnl_source=src, native_per_point=u, replay_cfg=cfg, setups=summ)
    json.dump(res, open(OUT / "compare_demo.json", "w"), indent=1, default=float)
    P.to_csv(OUT / "compare_demo_pairs.csv", index=False)

    print(f"window {a.t0} → {a.t1}  tf {a.tf}  P&L from {src}  (1 pt at base lot = {u:g} native)")
    for s, v in summ.items():
        print(f"{s:9s} demo {v['demo_cycles']:4d} cyc {v['demo_net_pts']:8.1f} pts PF {v['demo_pf']:5}  | "
              f"replay {v['replay_cycles']:4d} cyc {v['replay_net_pts']:8.1f} pts PF {v['replay_pf']:5}  | "
              f"matched {v['matched']} ({v['match_rate']:.0%}) corr {v['matched_corr']} "
              f"same-exit {v['same_exit_rate']}")
    m = P.dropna(subset=["demo_pts", "replay_pts"]).assign(gap=lambda x: x.demo_pts - x.replay_pts)
    if len(m):
        print("\nlargest gaps (demo − replay, pts):")
        w = m.reindex(m.gap.abs().sort_values(ascending=False).index).head(8)
        for r in w.itertuples():
            print(f"  {r.strat:9s} {pd.Timestamp(r.demo_ts, unit='s'):%m-%d %H:%M}  demo {r.demo_pts:7.1f} "
                  f"({r.demo_exit})  replay {r.replay_pts:7.1f} ({r.replay_exit})")
    return res


if __name__ == "__main__":
    main()
