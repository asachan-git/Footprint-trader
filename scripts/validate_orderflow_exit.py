#!/usr/bin/env python3
"""Offline validation: order-flow EXIT vs fixed TP (Republic).

For each absorption trigger (entry = trigger-bar close, SL = 1.5×ATR — settled),
walk the FORWARD bars one at a time and fire a full exit on the first bar that
shows the user's order-flow exit:

  (price reaches the opposite-side HVN  OR  absorption fires at an LVN)
   AND  CVD turns against the position (delta spike the other way)
   AND  volume elevated

Profile (HVN/LVN) is built STATIC as-of the entry bar from a rolling window of
footprint ladders, so the resting nodes are known before the trade — no lookahead.

Scored against baselines on the SAME triggers, proper per-bar SL/TP sequencing:
  fixed_2R   : TP = entry ± 2×(SL dist)         (RR target)
  absorp     : exit on any opposite absorption past 0.95×TP (mirrors live ingest)
  oracle_MFE : exit exactly at forward MFE        (ceiling)
  oflow      : the order-flow exit above

    python scripts/validate_orderflow_exit.py
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

from pipeline.footprint import build as build_fp
from pipeline.features.absorption import detect_absorption
from pipeline.features.atr import atr as _atr

SL_ATR = 1.5
FWD_N = 16            # forward bars to manage the trade (4h on 15m)
PROFILE_LB = 48       # bars of footprint for the HVN/LVN profile
HVN_PCTL = 0.80       # bucket vol >= this pctile = HVN
LVN_PCTL = 0.25       # <= this = LVN
MIN_TP_DIST = 1.0     # HVN must be >= this ×ATR beyond entry to count as a target
VOL_MULT = 1.2        # exit bar volume >= this × recent median
CVD_RATIO = 0.25      # exit-bar |delta|/vol >= this, opposing trade = CVD turn


def _bar_vol(b) -> float:
    return sum(l.vol for l in b.bid_ladder) + sum(l.vol for l in b.ask_ladder)


def _profile(bars, i, atr) -> tuple[list[float], list[tuple[float, float]]]:
    """Static volume profile from bars[i-PROFILE_LB:i+1]. Returns (hvn_levels, lvn_bands)."""
    bucket = max(atr * 0.10, 1e-6)
    vol: dict[int, float] = {}
    for b in bars[max(0, i - PROFILE_LB):i + 1]:
        for l in list(b.bid_ladder) + list(b.ask_ladder):
            vol[round(l.price / bucket)] = vol.get(round(l.price / bucket), 0.0) + l.vol
    if not vol:
        return [], []
    vals = sorted(vol.values())
    hi = vals[int(HVN_PCTL * (len(vals) - 1))]
    lo = vals[int(LVN_PCTL * (len(vals) - 1))]
    hvn = [k * bucket for k, v in vol.items() if v >= hi]
    lvn = [(k * bucket - bucket / 2, k * bucket + bucket / 2) for k, v in vol.items() if v <= lo]
    return sorted(hvn), lvn


def _in_lvn(price, lvn_bands) -> bool:
    return any(lo <= price <= hi for lo, hi in lvn_bands)


def _oflow_exit(side, entry, atr, fwd, hvn, lvn):
    """Return (exit_price, bar_offset, reason) for the order-flow exit, or None."""
    cvd = 0.0
    recent_vols = []
    for k, b in enumerate(fwd):
        v = _bar_vol(b)
        recent_vols.append(v)
        d = b.delta or 0.0
        cvd += d
        prof_ext = b.ohlc.h if side == "long" else b.ohlc.l
        # 1. opposite HVN reached (next resting node in profit dir, ≥ MIN_TP_DIST)
        reached_hvn = any(
            (side == "long" and entry + MIN_TP_DIST * atr <= lv <= prof_ext) or
            (side == "short" and prof_ext <= lv <= entry - MIN_TP_DIST * atr)
            for lv in hvn
        )
        # 2. absorption at an LVN, opposing side (exhaustion of the move)
        fp = build_fp(b)
        opp = "sell" if side == "long" else "buy"
        absorp_lvn = any(a.side == opp and _in_lvn(a.price, lvn)
                         for a in detect_absorption(b, fp, absorb_ratio=0.20))
        # 3. CVD turn against position on this bar + 4. elevated volume
        med = st.median(recent_vols) if recent_vols else v
        cvd_turn = ((d < 0) if side == "long" else (d > 0)) and abs(d) / max(v, 1e-9) >= CVD_RATIO
        vol_ok = v >= VOL_MULT * med
        if (reached_hvn or absorp_lvn) and cvd_turn and vol_ok:
            px = min(b.ohlc.c, prof_ext) if side == "long" else max(b.ohlc.c, prof_ext)
            why = "hvn" if reached_hvn else "lvn_absorp"
            return px, k, why
    return None


def _seq_fixed_tp(side, entry, sl, tp, fwd):
    """Per-bar SL/TP sequencing. SL wins on same-bar tie (conservative)."""
    for b in fwd:
        hit_sl = (side == "long" and b.ohlc.l <= sl) or (side == "short" and b.ohlc.h >= sl)
        hit_tp = (side == "long" and b.ohlc.h >= tp) or (side == "short" and b.ohlc.l <= tp)
        if hit_sl:
            return sl
        if hit_tp:
            return tp
    return fwd[-1].ohlc.c if fwd else entry


def run():
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
                entry = bars[i].ohlc.c
                fwd = bars[i + 1:i + 1 + FWD_N]
                if len(fwd) < 3:
                    continue
                sl = entry - SL_ATR * atr if side == "long" else entry + SL_ATR * atr
                risk = SL_ATR * atr
                hvn, lvn = _profile(bars, i, atr)

                def R(px):
                    return (px - entry) / risk if side == "long" else (entry - px) / risk

                # check SL before the order-flow exit fires (per-bar)
                of = _oflow_exit(side, entry, atr, fwd, hvn, lvn)
                sl_off = next((k for k, b in enumerate(fwd)
                               if (side == "long" and b.ohlc.l <= sl) or
                                  (side == "short" and b.ohlc.h >= sl)), None)
                if of is not None and (sl_off is None or of[1] <= sl_off):
                    of_R = R(of[0]); of_reason = of[2]
                elif sl_off is not None:
                    of_R = -1.0; of_reason = "sl"
                else:
                    of_R = R(fwd[-1].ohlc.c); of_reason = "timeout"   # flat close at window end

                tp = entry + 2 * risk if side == "long" else entry - 2 * risk
                fixed_R = R(_seq_fixed_tp(side, entry, sl, tp, fwd))
                # oracle: best close-to extreme
                mfe_px = max(b.ohlc.h for b in fwd) if side == "long" else min(b.ohlc.l for b in fwd)
                rows.append(dict(conf=r["strat_confirm"], vr=r["vol_ratio"],
                                 of_R=of_R, of_reason=of_reason,
                                 fixed_R=fixed_R, oracle_R=R(mfe_px)))
    return rows


def summ(rs, tag):
    if not rs:
        print(f"{tag}: n=0"); return
    n = len(rs)
    def line(key):
        R = [r[key] for r in rs]
        w = sum(1 for x in R if x > 0)
        return f"exp={st.mean(R):+.3f}R sum={sum(R):+6.1f} WR={100*w/n:3.0f}%"
    print(f"\n=== {tag} (n={n}) ===")
    print(f"  fixed 2R   : {line('fixed_R')}")
    print(f"  order-flow : {line('of_R')}")
    print(f"  oracle MFE : {line('oracle_R')}")
    reasons = {}
    for r in rs:
        reasons[r["of_reason"]] = reasons.get(r["of_reason"], 0) + 1
    print(f"  oflow exit reasons: {reasons}")


if __name__ == "__main__":
    rows = run()
    summ(rows, "ALL triggers")
    summ([r for r in rows if r["conf"]], "strat_confirm=True")
    summ([r for r in rows if r["vr"] >= 3.0], "climax vol>=3x")
    summ([r for r in rows if r["conf"] and r["vr"] >= 3.0], "confirm + climax")
