#!/usr/bin/env python3
"""Congress gate study — does VP / CVD / VA-expansion / FVG / retrace-quality
lift the R of congress-style fills?

Congress = vote direction → arm a LIMIT at the aggression-side stacked imbalance
→ fill on a retrace TOUCH within entry_expiry_bars → SL just beyond the ignition
base → target supplied downstream. This script replays that arm→touch→fill loop
over history (BTC+XAU, 15m), then for every FILL computes 5 candidate gates using
ONLY information available at/▶before the fill (no lookahead), and forward-sims
the trade to a fixed SL/2R outcome. It then buckets fills by each gate (pass/fail)
and reports n / win% / mean-R / PF, plus the all-gates-pass intersection.

Gates
  hvn      entry level sits in/near a trailing-VP HVN zone (real S/R, not vacuum)
  cvd      trailing CVD slope agrees with the vote side
  expand   ATR expanding vs ~10 bars ago (room for the tight-stop high-RR setup)
  fvg      a same-direction FVG overlaps the entry level
  retrace  pullback leg carries LESS vol & |delta| than the impulse (healthy pullback)

    .venv/bin/python -u scripts/congress_vp_study.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging; logging.disable(logging.CRITICAL)

import importlib.util
def _L(n, p):
    s = importlib.util.spec_from_file_location(n, ROOT / p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
_obs = _L("absorption_observations", "scripts/absorption_observations.py")

import pipeline.state_store as _ss
import execution.direction_engine as _de
import strategies.democracy as _demomod
from strategies.democracy import Democracy
from pipeline.footprint import build as build_fp
from pipeline.features.stacked_imbalance import stacked_imbalances
from pipeline.features.atr import atr
from pipeline.features import volume_profile as _vp
from pipeline.features.fvg import detect_fvgs

# ── config ──────────────────────────────────────────────────────────────────
TF = "15m"
WARMUP = 150          # vote needs ~100 bars; VP window needs ~96
EXPIRY = 3            # entry_expiry_bars
MIN_SL_MULT = 0.5     # SL floor = MIN_SL_MULT * ATR15 beyond the zone
HORIZON = 24          # forward-sim bars (6h on 15m)
TP_R = 2.0            # +2R target (matches fib_ext 2.0 used elsewhere)
VP_WIN = 96           # trailing bars for VP HVN computation (~1 day of 15m)
CVD_WIN = 5           # trailing bars for CVD slope
EXPAND_LOOKBACK = 10  # ATR expansion comparison window
SYMBOLS = ("BTCUSDT", "XAUTUSDT")


class _FakeStore:
    _bars: list = []
    def recent(self, symbol, tf, n): return self._bars[-n:]


def _bar_vol(bar) -> float:
    t = sum(l.vol for l in bar.bid_ladder) + sum(l.vol for l in bar.ask_ladder)
    return t if t > 0 else abs(bar.delta or 0.0)


def _agg_zone(bar, side):
    want = "buy" if side == "long" else "sell"
    zones = [z for z in stacked_imbalances(build_fp(bar), min_stack=3, ratio=3.0) if z.side == want]
    return max(zones, key=lambda z: z.count) if zones else None


def _entry_level(bar, side, mode):
    z = _agg_zone(bar, side)
    if z is None:
        return None, None
    if mode == "imb_lvn":
        fp = build_fp(bar)
        cells = [c for c in fp.cells if c.total > 0 and z.price_low <= c.price <= z.price_high]
        if cells:
            return min(cells, key=lambda c: c.total).price, z
    return (z.price_low if side == "long" else z.price_high), z


# ── gate features (no lookahead beyond the fill bar) ────────────────────────
def _hvn_hit(bars_upto_signal, level, atr15, sym):
    win = bars_upto_signal[-VP_WIN:]
    if len(win) < 20:
        return False
    bs = _vp.DEFAULT_BIN_SIZE.get(sym)
    vp = _vp.compute(win, "cached", win[-1].ohlc.c, bin_size=bs)
    tol = 0.25 * atr15
    for z in vp.hvn_zones:
        if z["low"] - tol <= level <= z["high"] + tol:
            return True
    return False


def _cvd_agree(bars_upto_signal, side):
    # Historical bars often have cvd_close=None; rebuild CVD slope from per-bar
    # delta (cumulative-delta proxy) over the trailing window instead.
    win = bars_upto_signal[-CVD_WIN:]
    if len(win) < 2:
        return False
    slope = sum((b.delta or 0.0) for b in win)
    return slope > 0 if side == "long" else slope < 0


def _expanding(bars_upto_signal):
    if len(bars_upto_signal) < WARMUP:
        return False
    now = atr(bars_upto_signal)
    prev = atr(bars_upto_signal[:-EXPAND_LOOKBACK])
    return prev > 0 and now > prev * 1.05


def _fvg_overlap(bars_upto_signal, level, side):
    fvgs = detect_fvgs(bars_upto_signal, max_age_bars=100)
    want = "bull" if side == "long" else "bear"
    return any(f.side == want and f.low <= level <= f.high for f in fvgs)


def _retrace_quality(signal_bar, retrace_bars):
    """Healthy pullback = retrace leg carries less vol AND less |delta| than impulse."""
    if not retrace_bars:
        return False
    imp_v = _bar_vol(signal_bar); imp_d = abs(signal_bar.delta or 0.0)
    rt_v = sum(_bar_vol(b) for b in retrace_bars)
    rt_d = sum(abs(b.delta or 0.0) for b in retrace_bars)
    return imp_v > 0 and rt_v < imp_v and rt_d < max(imp_d, 1e-9)


# ── forward outcome: SL-first vs +2R, else close at horizon (in R units) ────
def _outcome_R(bars, fill_idx, side, entry, sl):
    risk = (entry - sl) if side == "long" else (sl - entry)
    if risk <= 0:
        return None
    tp = entry + TP_R * risk if side == "long" else entry - TP_R * risk
    end = min(len(bars), fill_idx + HORIZON + 1)
    for k in range(fill_idx, end):
        o = bars[k].ohlc
        if side == "long":
            if o.l <= sl:   return -1.0           # SL-first (conservative)
            if o.h >= tp:   return TP_R
        else:
            if o.h >= sl:   return -1.0
            if o.l <= tp:   return TP_R
    c = bars[end - 1].ohlc.c
    return (c - entry) / risk if side == "long" else (entry - c) / risk


# ── replay congress fills ───────────────────────────────────────────────────
def collect_fills(mode):
    fake = _FakeStore()
    _ss.store = lambda: fake; _de.store = lambda: fake; _demomod.store = lambda: fake
    demo = Democracy(config={"symbols": list(SYMBOLS), "vote_tf": TF})
    fills = []
    for sym in SYMBOLS:
        bars = _obs.load_bars(sym, TF)
        if len(bars) < WARMUP + HORIZON + EXPIRY + 2:
            continue
        i = WARMUP
        while i < len(bars) - HORIZON - 1:
            sig = bars[i]
            fake._bars = bars[:i + 1]
            d = demo.decide(sym, TF, sig, {})
            if d is None or d.side == "flat":
                i += 1; continue
            side = d.side
            level, _z = _entry_level(sig, side, mode)
            if level is None:
                i += 1; continue
            # real pullback only (long below close / short above)
            if (side == "long" and level >= sig.ohlc.c) or (side == "short" and level <= sig.ohlc.c):
                i += 1; continue
            # arm: wait <= EXPIRY bars for a touch
            fill_idx = None
            for j in range(i + 1, min(i + 1 + EXPIRY, len(bars))):
                o = bars[j].ohlc
                if o.l <= level <= o.h:
                    fill_idx = j; break
            if fill_idx is None:
                i += 1; continue
            # SL beyond the ignition base of the signal bar (+ ATR floor)
            z = _agg_zone(sig, side)
            atr15 = atr(bars[:i + 1])
            floor = max(MIN_SL_MULT * atr15, 1e-9)
            if z is None:
                i += 1; continue
            sl = (z.price_low - floor) if side == "long" else (z.price_high + floor)
            r = _outcome_R(bars, fill_idx, side, level, sl)
            if r is None:
                i += 1; continue
            upto = bars[:i + 1]
            retrace_bars = bars[i + 1:fill_idx + 1]
            fills.append({
                "sym": sym, "side": side, "R": r, "bias": d.bias_strength,
                "hvn": _hvn_hit(upto, level, atr15, sym),
                "cvd": _cvd_agree(upto, side),
                "expand": _expanding(upto),
                "fvg": _fvg_overlap(upto, level, side),
                "retrace": _retrace_quality(sig, retrace_bars),
            })
            i = fill_idx + 1   # no overlapping fills
    return fills


def _stats(rows):
    n = len(rows)
    if n == 0:
        return "n=0"
    rs = [x["R"] for x in rows]
    wins = sum(1 for r in rs if r > 0)
    gp = sum(r for r in rs if r > 0); gl = -sum(r for r in rs if r < 0)
    pf = (gp / gl) if gl > 0 else float("inf")
    return f"n={n:3d}  win={wins/n*100:4.1f}%  meanR={sum(rs)/n:+5.2f}  PF={pf:4.2f}"


def report(mode):
    fills = collect_fills(mode)
    print(f"\n===== CONGRESS GATE STUDY  (mode={mode}, {TF}, horizon={HORIZON}, TP={TP_R}R) =====")
    print(f"BASELINE all fills:        {_stats(fills)}")
    if not fills:
        return
    print("\n-- single gates (pass vs fail) --")
    for g in ("hvn", "cvd", "expand", "fvg", "retrace"):
        p = [x for x in fills if x[g]]; f = [x for x in fills if not x[g]]
        print(f"  {g:8s} PASS  {_stats(p)}")
        print(f"  {g:8s} fail  {_stats(f)}")
    # combos
    print("\n-- combinations --")
    allg = [x for x in fills if all(x[g] for g in ("hvn", "cvd", "expand", "fvg", "retrace"))]
    print(f"  ALL 5 pass               {_stats(allg)}")
    core = [x for x in fills if x["hvn"] and x["cvd"] and x["retrace"]]
    print(f"  hvn+cvd+retrace          {_stats(core)}")
    hv_rt = [x for x in fills if x["hvn"] and x["retrace"]]
    print(f"  hvn+retrace              {_stats(hv_rt)}")
    cvd_rt = [x for x in fills if x["cvd"] and x["retrace"]]
    print(f"  cvd+retrace              {_stats(cvd_rt)}")
    # gate prevalence
    print("\n-- gate prevalence (% of fills passing) --")
    for g in ("hvn", "cvd", "expand", "fvg", "retrace"):
        print(f"  {g:8s} {sum(1 for x in fills if x[g])/len(fills)*100:4.1f}%")


if __name__ == "__main__":
    print("=== CONGRESS VP/CVD/VA/FVG/RETRACE GATE STUDY ===")
    for mode in ("imb_start", "imb_lvn"):
        report(mode)
