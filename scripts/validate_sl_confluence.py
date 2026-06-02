#!/usr/bin/env python3
"""Validate the structural SL variant: stop above the sell zone (candle extreme),
and whether VP/LVN confluence improves it. Entry = honest close (with the sellers).

  atr_sl      : SL = entry ∓ 1.5×ATR                (current Republic)
  struct_sl   : SL = candle extreme ± buffer        (above where sellers stacked)
  struct_conf : struct_sl, but only TAKE the trade when that stop level is in
                confluence with a rolling-profile HVN level OR sits in an LVN band
                (user's thesis: the stop works when it lines up with VP structure)

Profile (HVN/LVN) built static as-of the trigger from PROFILE_LB bars — no lookahead.
TP = RR × risk. Per-bar SL/TP sequencing over MANAGE_N forward bars.

    python scripts/validate_sl_confluence.py --buf 0.25 --rr 2.0 --conf 0.25
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "absorption_observations", ROOT / "scripts" / "absorption_observations.py")
_obs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_obs)
_ex_spec = importlib.util.spec_from_file_location(
    "validate_orderflow_exit", ROOT / "scripts" / "validate_orderflow_exit.py")
_ex = importlib.util.module_from_spec(_ex_spec)
_ex_spec.loader.exec_module(_ex)   # reuse _profile / _in_lvn

from pipeline.features.atr import atr as _atr

MANAGE_N = 16
DEFAULT_BUF = 0.25
DEFAULT_RR = 2.0
DEFAULT_CONF = 0.25   # SL within conf×ATR of an HVN = confluence


def _seq(side, entry, sl, tp, fwd):
    risk = abs(entry - sl)
    for x in fwd:
        hit_sl = (side == "short" and x.ohlc.h >= sl) or (side == "long" and x.ohlc.l <= sl)
        hit_tp = (side == "short" and x.ohlc.l <= tp) or (side == "long" and x.ohlc.h >= tp)
        if hit_sl:
            return -1.0
        if hit_tp:
            return abs(tp - entry) / risk
    last = fwd[-1].ohlc.c if fwd else entry
    return (entry - last) / risk if side == "short" else (last - entry) / risk


def run(buf, rr, conf):
    rows = []
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, "15m")
        idx = {b.close_ts: k for k, b in enumerate(bars)}
        for mode in ("momentum", "reversal"):
            for r in _obs.scan(sym, "15m", mode):
                i = idx.get(r["trigger_ts"])
                if i is None:
                    continue
                atr = _atr(bars[:i + 1]) or 0.0
                if atr <= 0:
                    continue
                side = r["winner"]
                b = bars[i]
                entry = b.ohlc.c
                extreme = b.ohlc.h if side == "short" else b.ohlc.l
                sl_struct = extreme + buf * atr if side == "short" else extreme - buf * atr
                sl_atr = entry + 1.5 * atr if side == "short" else entry - 1.5 * atr
                fwd = bars[i + 1:i + 1 + MANAGE_N]
                if len(fwd) < 3:
                    continue
                # confluence: stop level near an HVN, or inside an LVN band
                hvn, lvn = _ex._profile(bars, i, atr)
                near_hvn = any(abs(sl_struct - lv) <= conf * atr for lv in hvn)
                in_lvn = _ex._in_lvn(sl_struct, lvn)
                confluent = near_hvn or in_lvn

                def scored(sl):
                    risk = abs(entry - sl)
                    if risk <= 0:
                        return None
                    tp = entry - rr * risk if side == "short" else entry + rr * risk
                    return _seq(side, entry, sl, tp, fwd)

                rows.append(dict(conf=r["strat_confirm"], vr=r["vol_ratio"],
                                 confluent=confluent,
                                 atr_R=scored(sl_atr), struct_R=scored(sl_struct),
                                 struct_dist=abs(entry - sl_struct) / atr))
    return rows


def summ(rs, tag):
    rs = [r for r in rs if r["atr_R"] is not None and r["struct_R"] is not None]
    if not rs:
        print(f"{tag}: n=0"); return
    n = len(rs)
    def e(key, sub=None):
        s = [r[key] for r in rs if (sub is None or r["confluent"])]
        return (st.mean(s), len(s)) if s else (0.0, 0)
    a, _ = e("atr_R"); s, _ = e("struct_R")
    sc, nc = e("struct_R", "conf")
    print(f"\n=== {tag} (n={n}) ===")
    print(f"  atr 1.5×     : exp={a:+.3f}R")
    print(f"  struct (all) : exp={s:+.3f}R  median_dist={st.median(r['struct_dist'] for r in rs):.2f}×ATR")
    print(f"  struct+CONFLUENCE only : exp={sc:+.3f}R  (n={nc}/{n} confluent)")


def main(argv):
    buf, rr, conf = DEFAULT_BUF, DEFAULT_RR, DEFAULT_CONF
    if "--buf" in argv: buf = float(argv[argv.index("--buf") + 1])
    if "--rr" in argv: rr = float(argv[argv.index("--rr") + 1])
    if "--conf" in argv: conf = float(argv[argv.index("--conf") + 1])
    print(f"params: buf={buf}×ATR  rr={rr}  conf_tol={conf}×ATR  (entry=close)")
    rows = run(buf, rr, conf)
    summ(rows, "ALL")
    summ([r for r in rows if r["conf"]], "strat_confirm=True")
    summ([r for r in rows if r["vr"] >= 3.0], "climax vol>=3x")


if __name__ == "__main__":
    main(sys.argv[1:])
