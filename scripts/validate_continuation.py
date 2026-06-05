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
from pipeline.features.volume_profile import compute as _vp_compute, DEFAULT_BIN_SIZE

W = 20            # CVD window
MANAGE_N = 16
MIN_HVN_DIST = 1.0  # HVN target must be ≥ this ×ATR beyond entry


def _bar_vol(b): return sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)


# ── HVN/LVN level source ──────────────────────────────────────────────────────
# "rolling" = _ex._profile (48-bar trailing window). "session" = the developing
# session-anchored daily VP (XAU 03:30 / BTC 05:30 IST), bars from this session up
# to i only (no lookahead) → pipeline volume_profile zones. Same output shape:
# (hvn_levels: sorted prices, lvn_bands: [(lo,hi)]).
def _session_start_sec(symbol): return 12600 if symbol.startswith("XAU") else 19800
def _session_key(ts, symbol):   return (ts + 19800 - _session_start_sec(symbol)) // 86400


def _session_profile(bars, i, symbol, atr):
    key = _session_key(bars[i].close_ts, symbol)
    seg = [bars[j] for j in range(max(0, i - 200), i + 1)
           if _session_key(bars[j].close_ts, symbol) == key]
    if len(seg) < 20:
        return [], []
    try:
        vp = _vp_compute(seg, "intraday", bars[i].ohlc.c, bin_size=DEFAULT_BIN_SIZE.get(symbol))
    except Exception:
        return [], []
    hvn = sorted(((z["low"] + z["high"]) / 2) for z in (vp.hvn_zones or []))
    lvn = [(z["low"], z["high"]) for z in (vp.lvn_zones or [])]
    return hvn, lvn


def _profile(vp_src, bars, i, symbol, atr):
    if vp_src == "session":
        return _session_profile(bars, i, symbol, atr)
    return _ex._profile(bars, i, atr)


def run(dr, vol_mult, vp_src="rolling"):
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
            hvn, lvn = _profile(vp_src, bars, i, sym, atr)
            # HVN target beyond entry in trade dir, ≥ MIN_HVN_DIST
            tcand = [h for h in hvn if (side == "long" and h >= entry + MIN_HVN_DIST * atr)
                     or (side == "short" and h <= entry - MIN_HVN_DIST * atr)]
            target = (min(tcand) if side == "long" else max(tcand)) if tcand else None
            # LVN stop: nearest LVN against the trade (below for long), SL just beyond it
            lcand = [ (lo+hi)/2 for lo,hi in lvn if (side=="long" and (lo+hi)/2 < entry) or (side=="short" and (lo+hi)/2 > entry)]
            lvn_stop = (max(lcand) if side == "long" else min(lcand)) if lcand else None
            # HVN-SUPPORT stop: nearest HVN against the trade (below for long), SL just
            # beyond it. HVN is the real S/R that should hold the continuation.
            hcand = [h for h in hvn if (side=="long" and h < entry) or (side=="short" and h > entry)]
            hvn_sup = (max(hcand) if side == "long" else min(hcand)) if hcand else None
            hvn_stop = None if hvn_sup is None else (hvn_sup - 0.1*atr if side == "long" else hvn_sup + 0.1*atr)
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
                             has_hvn_sup=hvn_stop is not None,
                             R_lvn=seq(lvn_stop, target), R_atr=seq(atr_stop, target),
                             R_hvn=seq(hvn_stop, target)))
    return rows


def run_rev(tol_mult=0.15, vp_src="rolling"):
    """C4 — HVN-EXTREME reversal: a bar tags an HVN level (high reaches an HVN from
    below = resistance → SHORT fade; low reaches one from above = support → LONG fade).
    Target = next opposing HVN beyond entry; stop = just past the tagged HVN. Tests the
    other half of the model: price stalls/reverses AT HVNs (LVN is the travel between)."""
    rows = []
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, "15m")
        if len(bars) < W + MANAGE_N + 2:
            continue
        for i in range(W, len(bars) - MANAGE_N - 1):
            b = bars[i]
            atr = _atr(bars[:i + 1]) or 0.0
            if atr <= 0:
                continue
            tol = tol_mult * atr
            hvn, lvn = _profile(vp_src, bars, i, sym, atr)
            if not hvn:
                continue
            prev_c = bars[i - 1].ohlc.c
            # resistance tag: high reaches an HVN that sits above the prior close → SHORT
            res = [h for h in hvn if b.ohlc.h >= h - tol and h > prev_c]
            sup = [h for h in hvn if b.ohlc.l <= h + tol and h < prev_c]
            if res and not sup:
                side, wall = "short", min(res)
            elif sup and not res:
                side, wall = "long", max(sup)
            else:
                continue
            entry = b.ohlc.c
            rng = max(b.ohlc.h - b.ohlc.l, 1e-9)
            d = b.delta or 0.0
            # arrival THROUGH an LVN: a vacuum band sits between prior close and the
            # tagged HVN (price travelled the LVN to reach the wall).
            lmids = [(lo+hi)/2 for lo, hi in lvn]
            thru_lvn = any(prev_c < m < wall for m in lmids) if side == "short" \
                else any(wall < m < prev_c for m in lmids)
            # delta EXHAUSTION at the wall: rejection wick back off the extreme + the
            # bar's delta NOT confirming the push into the wall (buyers spent at a top /
            # sellers spent at a bottom).
            if side == "short":
                exhaust = (b.ohlc.h - b.ohlc.c) / rng >= 0.4 and d <= 0
            else:
                exhaust = (b.ohlc.c - b.ohlc.l) / rng >= 0.4 and d >= 0
            # target = nearest opposing HVN beyond entry in the fade direction
            tcand = [h for h in hvn if (side == "long" and h >= entry + MIN_HVN_DIST * atr)
                     or (side == "short" and h <= entry - MIN_HVN_DIST * atr)]
            target = (min(tcand) if side == "long" else max(tcand)) if tcand else None
            stop = wall + 0.1*atr if side == "short" else wall - 0.1*atr
            fwd = bars[i+1:i+1+MANAGE_N]
            if side == "long":
                mfe = (max(x.ohlc.h for x in fwd) - entry)/atr
                mae = (entry - min(x.ohlc.l for x in fwd))/atr
            else:
                mfe = (entry - min(x.ohlc.l for x in fwd))/atr
                mae = (max(x.ohlc.h for x in fwd) - entry)/atr
            def seq(stp, tp):
                if stp is None or tp is None: return None
                risk = abs(entry-stp)
                if risk <= 0: return None
                for x in fwd:
                    hs=(side=="long" and x.ohlc.l<=stp) or (side=="short" and x.ohlc.h>=stp)
                    ht=(side=="long" and x.ohlc.h>=tp) or (side=="short" and x.ohlc.l<=tp)
                    if hs: return -1.0
                    if ht: return abs(tp-entry)/risk
                last=fwd[-1].ohlc.c
                return (last-entry)/risk if side=="long" else (entry-last)/risk
            rows.append(dict(sym=sym, side=side, mfe=mfe, mae=mae,
                             has_tgt=target is not None, thru_lvn=thru_lvn,
                             exhaust=exhaust, R=seq(stop, target)))
    return rows


def main(argv):
    dr = float(argv[argv.index("--dr")+1]) if "--dr" in argv else 0.35
    vol = float(argv[argv.index("--vol")+1]) if "--vol" in argv else 1.5
    vp_src = argv[argv.index("--vp")+1] if "--vp" in argv else "rolling"
    rows = run(dr, vol, vp_src)
    n=len(rows)
    print(f"VP source: {vp_src}  ({'session-anchored daily VP' if vp_src=='session' else '48-bar rolling window'})")
    print(f"high-delta bars: n={n}  (dr≥{dr}, vol≥{vol}×med)")
    ag=[r for r in rows if r["cvd_agree"]]; dis=[r for r in rows if not r["cvd_agree"]]
    def mm(rs,t):
        if not rs: print(f"  {t}: n=0"); return
        print(f"  {t:22} n={len(rs):4d}  MFE={st.mean(r['mfe'] for r in rs):.2f} MAE={st.mean(r['mae'] for r in rs):.2f}  MFE/MAE={st.mean(r['mfe'] for r in rs)/max(st.mean(r['mae'] for r in rs),1e-9):.2f}")
    print("\nC1 — high-delta continuation, CVD gate:")
    mm(rows,"ALL"); mm(ag,"CVD agrees"); mm(dis,"CVD disagrees")
    print(f"\nC2 — HVN target available: {sum(r['has_hvn'] for r in rows)}/{n}  | LVN stop available: {sum(r['has_lvn'] for r in rows)}/{n}")
    print("\nC3 — expectancy to next HVN (CVD-agree only):")
    for key,lab in (("R_hvn","HVN-support stop"),("R_lvn","LVN stop"),("R_atr","1.5×ATR stop")):
        vals=[r[key] for r in ag if r[key] is not None]
        if vals:
            w=sum(1 for x in vals if x>0)
            print(f"  {lab:16} n={len(vals):4d} exp={st.mean(vals):+.3f}R WR={100*w/len(vals):.0f}%")

    # C4 — HVN-extreme reversal (the other half of the model) + arrival/exhaustion filters
    rev = run_rev(vp_src=vp_src)
    print(f"\nC4 — HVN-extreme reversal (fade a tagged HVN → opposing HVN):  tags n={len(rev)}")
    def rev_stat(rs, lab):
        rv=[r["R"] for r in rs if r["R"] is not None]
        if not rv:
            print(f"  {lab:28} n=0"); return
        w=sum(1 for x in rv if x>0)
        med=st.median(rv)
        print(f"  {lab:28} n={len(rv):4d} exp={st.mean(rv):+.3f}R med={med:+.2f}R WR={100*w/len(rv):.0f}%")
    rev_stat(rev, "raw (any tag)")
    rev_stat([r for r in rev if r["thru_lvn"]], "arrived THROUGH LVN")
    rev_stat([r for r in rev if r["exhaust"]], "delta-exhaustion only")
    both=[r for r in rev if r["thru_lvn"] and r["exhaust"]]
    rev_stat(both, "THROUGH-LVN + exhaustion")
    for s in ("BTCUSDT","XAUTUSDT"):
        rev_stat([r for r in both if r["sym"]==s], f"  both · {s}")


if __name__ == "__main__":
    main(sys.argv[1:])
