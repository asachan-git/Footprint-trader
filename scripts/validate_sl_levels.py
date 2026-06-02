#!/usr/bin/env python3
"""Compare SL anchors on absorption triggers (entry = honest close):

  atr        : entry ∓ 1.5×ATR                              (baseline)
  stack_bt   : SL above the favorable footprint walls — stacked sell-imbalance
               (short) / buy-imbalance (long) + sell/buy big-trade prints on the
               stop side, beyond the highest/lowest such level + buffer
  vp         : SL at the nearest VP-HVN level on the stop side + buffer
  combined   : SL at a footprint wall that is ALSO in VP-HVN confluence

Each variant falls back to 1.5×ATR when its anchor is absent. TP = 2R × the
variant's own risk; per-bar SL/TP sequencing over MANAGE_N forward bars.

    python scripts/validate_sl_levels.py --buf 0.2 --rr 2.0 --conf 0.25
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
def _load(name, rel):
    s = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
_obs = _load("absorption_observations", "scripts/absorption_observations.py")
_ex = _load("validate_orderflow_exit", "scripts/validate_orderflow_exit.py")

from pipeline.footprint import build as build_fp
from pipeline.features.stacked_imbalance import stacked_imbalances
from pipeline.features.big_trade import detect_events
from pipeline.features.atr import atr as _atr

MANAGE_N = 16


def _walls(bars, i, side, entry):
    """Favorable footprint wall prices on the stop side (above entry for short)."""
    b = bars[i]; fp = build_fp(b)
    want = "sell" if side == "short" else "buy"
    out = []
    for z in stacked_imbalances(fp, min_stack=3, ratio=3.0):
        if z.side != want:
            continue
        edge = z.price_high if side == "short" else z.price_low
        if (side == "short" and edge >= entry) or (side == "long" and edge <= entry):
            out.append(edge)
    for e in detect_events(b, bars[max(0, i - 20):i], None):
        if e.aggressor != want:
            continue
        if (side == "short" and e.price >= entry) or (side == "long" and e.price <= entry):
            out.append(e.price)
    return out


def _nearest_hvn(hvn, side, entry):
    cand = [lv for lv in hvn if (side == "short" and lv >= entry) or (side == "long" and lv <= entry)]
    if not cand:
        return None
    return min(cand) if side == "short" else max(cand)


def _seq_R(side, entry, sl, rr, fwd):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp = entry - rr * risk if side == "short" else entry + rr * risk
    for x in fwd:
        hit_sl = (side == "short" and x.ohlc.h >= sl) or (side == "long" and x.ohlc.l <= sl)
        hit_tp = (side == "short" and x.ohlc.l <= tp) or (side == "long" and x.ohlc.h >= tp)
        if hit_sl:
            return -1.0
        if hit_tp:
            return rr
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
                side = r["winner"]; entry = bars[i].ohlc.c
                fwd = bars[i + 1:i + 1 + MANAGE_N]
                if len(fwd) < 3:
                    continue
                atr_sl = entry + 1.5 * atr if side == "short" else entry - 1.5 * atr
                walls = _walls(bars, i, side, entry)
                hvn, _ = _ex._profile(bars, i, atr)
                hvn_lvl = _nearest_hvn(hvn, side, entry)

                def beyond(level):
                    return level + buf * atr if side == "short" else level - buf * atr

                # stack/big-trade SL = beyond the farthest favorable wall
                if walls:
                    w = max(walls) if side == "short" else min(walls)
                    sl_stack, has_stack = beyond(w), True
                else:
                    sl_stack, has_stack = atr_sl, False
                # VP SL
                if hvn_lvl is not None:
                    sl_vp, has_vp = beyond(hvn_lvl), True
                else:
                    sl_vp, has_vp = atr_sl, False
                # combined: a wall that is also near an HVN
                comb = None
                for w in walls:
                    if any(abs(w - lv) <= conf * atr for lv in hvn):
                        comb = max(comb, w) if (comb is not None and side == "short") else \
                               (min(comb, w) if (comb is not None) else w)
                if comb is not None:
                    sl_comb, has_comb = beyond(comb), True
                else:
                    sl_comb, has_comb = atr_sl, False

                rows.append(dict(
                    conf=r["strat_confirm"], vr=r["vol_ratio"],
                    atr_R=_seq_R(side, entry, atr_sl, rr, fwd),
                    stack_R=_seq_R(side, entry, sl_stack, rr, fwd), has_stack=has_stack,
                    vp_R=_seq_R(side, entry, sl_vp, rr, fwd), has_vp=has_vp,
                    comb_R=_seq_R(side, entry, sl_comb, rr, fwd), has_comb=has_comb,
                ))
    return rows


def summ(rs, tag):
    rs = [r for r in rs if r["atr_R"] is not None]
    if not rs:
        print(f"{tag}: n=0"); return
    n = len(rs)
    def e(k, flag=None):
        s = [r[k] for r in rs if r[k] is not None and (flag is None or r[flag])]
        return st.mean(s) if s else 0.0
    cov = lambda f: sum(1 for r in rs if r[f])
    print(f"\n=== {tag} (n={n}) ===")
    print(f"  atr 1.5×        : {e('atr_R'):+.3f}R")
    print(f"  stack/big-trade : {e('stack_R'):+.3f}R  (anchor present {cov('has_stack')}/{n}; "
          f"on-anchor-only {e('stack_R','has_stack'):+.3f}R)")
    print(f"  VP-HVN          : {e('vp_R'):+.3f}R  (anchor present {cov('has_vp')}/{n}; "
          f"on-anchor-only {e('vp_R','has_vp'):+.3f}R)")
    print(f"  COMBINED        : {e('comb_R'):+.3f}R  (anchor present {cov('has_comb')}/{n}; "
          f"on-anchor-only {e('comb_R','has_comb'):+.3f}R)")


def main(argv):
    buf = float(argv[argv.index("--buf") + 1]) if "--buf" in argv else 0.2
    rr = float(argv[argv.index("--rr") + 1]) if "--rr" in argv else 2.0
    conf = float(argv[argv.index("--conf") + 1]) if "--conf" in argv else 0.25
    print(f"params: buf={buf}×ATR rr={rr} conf={conf}×ATR (entry=close)")
    rows = run(buf, rr, conf)
    summ(rows, "ALL")
    summ([r for r in rows if r["conf"]], "strat_confirm=True")
    summ([r for r in rows if r["vr"] >= 3.0], "climax vol>=3x")


if __name__ == "__main__":
    main(sys.argv[1:])
