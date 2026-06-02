#!/usr/bin/env python3
"""Offline validation: order-flow structural SL vs fixed 1.5×ATR (Republic).

Reuses the absorption trigger detection from absorption_observations.scan() and,
for each trigger candle, places the stop two ways:

  fixed      : entry ∓ 1.5×ATR              (today's Republic SL)
  structural : just beyond the absorbed extreme, widened to the far edge of any
               stacked-imbalance wall on the stop side, + small ATR buffer
               (the order-flow invalidation: price pushing back through the wall
                means the trapped side was right → thesis dead)

Then scores both with the same forward MFE/MAE the obs logger already computes,
so we can see the WR-vs-RR / premature-stop tradeoff on the real trigger set.

NO live state (vp_cache etc.) — everything is computed from the footprint bars,
so the numbers are reproducible from history alone.

    python scripts/validate_orderflow_sl.py
    python scripts/validate_orderflow_sl.py --buffer 0.10 --tf 15m
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging
logging.disable(logging.CRITICAL)

from pipeline.footprint import build as build_fp
from pipeline.features.stacked_imbalance import stacked_imbalances

# reuse the exact trigger detector + data loader so the candle set is identical
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "absorption_observations", ROOT / "scripts" / "absorption_observations.py")
_obs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_obs)
load_bars, PARAMS, CONT_N = _obs.load_bars, _obs.PARAMS, _obs.CONT_N

# default RR target used to score a win (TP = entry ± TP_RR × SL_dist), capped at
# the realized forward MFE (can't capture more than price actually offered).
TP_RR = 2.0
FIXED_SL_ATR = 1.5


def _structural_sl_dist_atr(bar, fp, winner: str, atr_v: float, buffer_atr: float) -> tuple[float, str]:
    """Distance (in ATR) from entry(close) to the structural stop, + a tag."""
    o, h, l, c = bar.ohlc.o, bar.ohlc.h, bar.ohlc.l, bar.ohlc.c
    # absorbed extreme = the wick the trapped side defended
    extreme = h if winner == "short" else l
    src = "extreme"
    # widen to the far edge of a stacked-imbalance wall on the stop side, if any
    zones = stacked_imbalances(fp)
    for z in zones:
        if winner == "short" and z.price_high >= extreme - atr_v * 0.05:
            if z.price_high > extreme:
                extreme = z.price_high
                src = f"stack{z.count}"
        elif winner == "long" and z.price_low <= extreme + atr_v * 0.05:
            if z.price_low < extreme:
                extreme = z.price_low
                src = f"stack{z.count}"
    sl = extreme + buffer_atr * atr_v if winner == "short" else extreme - buffer_atr * atr_v
    dist = abs(sl - c)
    return (dist / atr_v if atr_v > 0 else 0.0), src


def _score(sl_dist_atr: float, mfe_atr: float, mae_atr: float) -> float:
    """Realized R for a trade with stop at sl_dist and TP = TP_RR×sl_dist (capped
    at MFE). Ambiguous bars (both SL and TP reachable) resolved as SL (conservative).
    """
    if sl_dist_atr <= 0:
        return 0.0
    tp_atr = min(TP_RR * sl_dist_atr, mfe_atr)        # can't take more than offered
    tp_hit = mfe_atr >= tp_atr and tp_atr > 0
    sl_hit = mae_atr >= sl_dist_atr
    if tp_hit and not sl_hit:
        return tp_atr / sl_dist_atr                    # = TP_RR (or less if MFE-capped)
    if sl_hit:
        return -1.0                                    # SL first (conservative on tie)
    return mfe_atr / sl_dist_atr * 0.0                 # neither → flat (open, 0R)


def run(symbol: str, tf: str, buffer_atr: float) -> list[dict]:
    rows: list[dict] = []
    for mode in ("momentum", "reversal"):
        # absorption_observations.scan recomputes mfe/mae too; rerun the same gate
        for r in _obs.scan(symbol, tf, mode):
            if r["fwd_mfe_atr"] is None or r["fwd_mae_atr"] is None:
                continue
            # rebuild the trigger bar's fp for stacked imbalance (scan dropped it)
            bars = _BARS_CACHE.setdefault((symbol, tf), load_bars(symbol, tf))
            bar = next((b for b in bars if b.close_ts == r["trigger_ts"]), None)
            if bar is None:
                continue
            fp = build_fp(bar)
            atr_v = (abs(bar.ohlc.h - bar.ohlc.l))  # placeholder; real atr below
            # recover atr from obs: mfe_atr = mfe/atr → not invertible cleanly, so
            # recompute structural dist in *price* then divide by the same atr the
            # obs used. obs stored only ratios, so derive atr from the bar range is
            # wrong — instead recompute atr exactly like the obs did.
            from pipeline.features.atr import atr as _atr
            idx = bars.index(bar)
            atr_v = _atr(bars[:idx + 1]) or 0.0
            if atr_v <= 0:
                continue
            sdist, src = _structural_sl_dist_atr(bar, fp, r["winner"], atr_v, buffer_atr)
            if sdist <= 0:
                continue
            rows.append({
                **r,
                "fixed_R": _score(FIXED_SL_ATR, r["fwd_mfe_atr"], r["fwd_mae_atr"]),
                "struct_R": _score(sdist, r["fwd_mfe_atr"], r["fwd_mae_atr"]),
                "struct_sl_atr": round(sdist, 2), "struct_src": src,
            })
    return rows


_BARS_CACHE: dict = {}


def _summ(rows: list[dict], tag: str):
    if not rows:
        print(f"{tag}: n=0"); return
    n = len(rows)
    fR = [r["fixed_R"] for r in rows]; sR = [r["struct_R"] for r in rows]
    cont = [r for r in rows if r["continuation"]]
    # premature-stop rescue: continuation trades where fixed SL was hit (mae≥1.5)
    # but structural would have survived (mae < struct_dist)
    rescued = sum(1 for r in cont
                  if r["fwd_mae_atr"] >= FIXED_SL_ATR and r["fwd_mae_atr"] < r["struct_sl_atr"])
    tighter = sum(1 for r in rows if r["struct_sl_atr"] < FIXED_SL_ATR)
    print(f"\n=== {tag} (n={n}) ===")
    print(f"  fixed 1.5×ATR : expectancy {statistics.mean(fR):+.3f}R  "
          f"sum {sum(fR):+.1f}R  win {sum(1 for x in fR if x>0)}/{n}")
    print(f"  structural    : expectancy {statistics.mean(sR):+.3f}R  "
          f"sum {sum(sR):+.1f}R  win {sum(1 for x in sR if x>0)}/{n}")
    print(f"  struct SL dist: median {statistics.median(r['struct_sl_atr'] for r in rows):.2f}×ATR  "
          f"(tighter than 1.5× in {tighter}/{n})")
    print(f"  premature-stop RESCUE (cont trades fixed-SL killed, struct survived): "
          f"{rescued}/{len(cont)}")
    src = {}
    for r in rows:
        k = "stacked" if r["struct_src"].startswith("stack") else "extreme"
        src[k] = src.get(k, 0) + 1
    print(f"  SL anchor src : {src}")


def main(argv):
    symbols = ["BTCUSDT", "XAUTUSDT"]
    tf = "15m"; buffer_atr = 0.10
    if "--symbols" in argv: symbols = argv[argv.index("--symbols") + 1].split(",")
    if "--tf" in argv: tf = argv[argv.index("--tf") + 1]
    if "--buffer" in argv: buffer_atr = float(argv[argv.index("--buffer") + 1])

    allrows = []
    for s in symbols:
        rows = run(s, tf, buffer_atr)
        allrows.extend(rows)
        _summ(rows, f"{s} {tf}")
    _summ(allrows, f"ALL {tf} (buffer={buffer_atr}×ATR, TP={TP_RR}R)")
    # confirmed-only cut (the +EV bucket from the TP analysis)
    _summ([r for r in allrows if r["strat_confirm"]], f"ALL strat_confirm=True")


if __name__ == "__main__":
    main(sys.argv[1:])
