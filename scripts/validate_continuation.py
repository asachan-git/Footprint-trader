#!/usr/bin/env python3
"""Validate the footprint CONTINUATION playbook (for wave_fib upgrade):

  entry  : a HIGH-DELTA bar whose windowed CVD AGREES with the bar's delta
           (delta-direction continuation, order-flow confirmed)
  target : the next HVN beyond entry in the trade direction (HVN→HVN move)
  stop   : just beyond the nearest LVN against the trade (LVN = S/R that should
           hold for the continuation; break of it = thesis dead) — ATR fallback

Tests three claims independently + combined, on 15m history:
  C1  high-delta + CVD-agree continues (fwd MFE in delta dir)   vs CVD-disagree
  C2  HVN→HVN: price reaches the next HVN target (hit-rate)
  C3  LVN-stop expectancy vs fixed 1.5×ATR stop

    python scripts/validate_continuation.py --dr 0.35 --vol 1.5
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging; logging.disable(logging.CRITICAL)

import importlib.util
def _load(n, p):
    s = importlib.util.spec_from_file_location(n, ROOT / p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
_obs = _load("absorption_observations", "scripts/absorption_observations.py")
_ex = _load("validate_orderflow_exit", "scripts/validate_orderflow_exit.py")  # _profile, _in_lvn

from pipeline.footprint import build as build_fp
from pipeline.features.atr import atr as _atr

W = 20            # CVD window
MANAGE_N = 16
MIN_HVN_DIST = 1.0  # HVN target must be ≥ this ×ATR beyond entry


def _bar_vol(b): return sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)


def run(dr, vol_mult):
    rows = []
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, "15m")
        if len(bars) < W + MANAGE_N + 2:
            continue
        for i in range(W, len(bars) - MANAGE_N - 1):
            b = bars[i]
            v = _bar_vol(b)
            prev = [x for x in (_bar_vol(bars[j]) for j in range(i - W, i)) if x > 0]
            med = st.median(prev) if prev else 0
            d = b.delta or 0.0
            if med <= 0 or v < vol_mult * med:        # not a high-vol bar
                continue
            if abs(d) / max(v, 1e-9) < dr:            # not high-delta
                continue
            side = "long" if d > 0 else "short"
            cvd = sum((bars[j].delta or 0) for j in range(i - W, i + 1))
            cvd_agree = (cvd > 0) == (d > 0)
            atr = _atr(bars[:i + 1]) or 0.0
            if atr <= 0:
                continue
            entry = b.ohlc.c
            hvn, lvn = _ex._profile(bars, i, atr)
            # HVN target beyond entry in trade dir, ≥ MIN_HVN_DIST
            tcand = [h for h in hvn if (side == "long" and h >= entry + MIN_HVN_DIST * atr)
                     or (side == "short" and h <= entry - MIN_HVN_DIST * atr)]
            target = (min(tcand) if side == "long" else max(tcand)) if tcand else None
            # LVN stop: nearest LVN against the trade (below for long), SL just beyond it
            lcand = [ (lo+hi)/2 for lo,hi in lvn if (side=="long" and (lo+hi)/2 < entry) or (side=="short" and (lo+hi)/2 > entry)]
            lvn_stop = (max(lcand) if side == "long" else min(lcand)) if lcand else None
            atr_stop = entry - 1.5*atr if side == "long" else entry + 1.5*atr
            fwd = bars[i+1:i+1+MANAGE_N]
            # forward MFE/MAE in ATR
            if side == "long":
                mfe = (max(x.ohlc.h for x in fwd) - entry)/atr
                mae = (entry - min(x.ohlc.l for x in fwd))/atr
            else:
                mfe = (entry - min(x.ohlc.l for x in fwd))/atr
                mae = (max(x.ohlc.h for x in fwd) - entry)/atr
            # did price reach the HVN target before SL? (per-bar, LVN stop or atr fallback)
            def seq(stop, tp):
                if stop is None or tp is None: return None
                risk = abs(entry-stop)
                if risk<=0: return None
                for x in fwd:
                    hs=(side=="long" and x.ohlc.l<=stop) or (side=="short" and x.ohlc.h>=stop)
                    ht=(side=="long" and x.ohlc.h>=tp) or (side=="short" and x.ohlc.l<=tp)
                    if hs: return -1.0
                    if ht: return abs(tp-entry)/risk
                last=fwd[-1].ohlc.c
                return (last-entry)/risk if side=="long" else (entry-last)/risk
            rows.append(dict(sym=sym, side=side, cvd_agree=cvd_agree, mfe=mfe, mae=mae,
                             has_hvn=target is not None, has_lvn=lvn_stop is not None,
                             R_lvn=seq(lvn_stop, target), R_atr=seq(atr_stop, target)))
    return rows


def main(argv):
    dr = float(argv[argv.index("--dr")+1]) if "--dr" in argv else 0.35
    vol = float(argv[argv.index("--vol")+1]) if "--vol" in argv else 1.5
    rows = run(dr, vol)
    n=len(rows)
    print(f"high-delta bars: n={n}  (dr≥{dr}, vol≥{vol}×med)")
    ag=[r for r in rows if r["cvd_agree"]]; dis=[r for r in rows if not r["cvd_agree"]]
    def mm(rs,t):
        if not rs: print(f"  {t}: n=0"); return
        print(f"  {t:22} n={len(rs):4d}  MFE={st.mean(r['mfe'] for r in rs):.2f} MAE={st.mean(r['mae'] for r in rs):.2f}  MFE/MAE={st.mean(r['mfe'] for r in rs)/max(st.mean(r['mae'] for r in rs),1e-9):.2f}")
    print("\nC1 — high-delta continuation, CVD gate:")
    mm(rows,"ALL"); mm(ag,"CVD agrees"); mm(dis,"CVD disagrees")
    print(f"\nC2 — HVN target available: {sum(r['has_hvn'] for r in rows)}/{n}  | LVN stop available: {sum(r['has_lvn'] for r in rows)}/{n}")
    print("\nC3 — expectancy to next HVN (CVD-agree only):")
    for key,lab in (("R_lvn","LVN stop"),("R_atr","1.5×ATR stop")):
        vals=[r[key] for r in ag if r[key] is not None]
        if vals:
            w=sum(1 for x in vals if x>0)
            print(f"  {lab:14} n={len(vals):4d} exp={st.mean(vals):+.3f}R WR={100*w/len(vals):.0f}%")


if __name__ == "__main__":
    main(sys.argv[1:])
