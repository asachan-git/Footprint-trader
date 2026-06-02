#!/usr/bin/env python3
"""Validate the 'enter with the big guys' model on absorption triggers.

Old model (refuted): enter at trigger-bar CLOSE, SL = 1.5×ATR from close.
New model (this):     enter as a LIMIT at the high selling/buying zone (the
                      institutional wall on the trigger candle), filled only if a
                      forward bar RETESTS the zone; SL just beyond the zone (tight,
                      because if price breaks the wall the big guys were wrong);
                      exit at an RR target.

Zone = the canonical absorption price on the trigger candle (the wall), fallback
to the absorbed extreme (high for short / low for long).

Measures fill-rate (how often the retest taps the zone) + expectancy, and the SL
risk is now measured from the ZONE entry, not a close 1×ATR away — the whole
point of the user's model.

    python scripts/validate_zone_entry.py
    python scripts/validate_zone_entry.py --buf 0.3 --retest 4 --rr 2.0
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
from pipeline.features.absorption import detect_canonical_absorption
from pipeline.features.atr import atr as _atr

RETEST_N = 4      # bars allowed for the limit to be tapped
MANAGE_N = 16     # bars to manage after fill
DEFAULT_BUF = 0.3 # SL buffer beyond the zone, ×ATR
DEFAULT_RR = 2.0


def _zone(bar, fp, side, mode):
    """High selling zone (short) / high buying zone (long) = absorption wall."""
    a = detect_canonical_absorption(bar, fp, mode=mode)
    if a:
        return a[-1].price
    return bar.ohlc.h if side == "short" else bar.ohlc.l


def run(buf, retest, rr):
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
                zone = _zone(b, build_fp(b), side, mode)
                sl = zone + buf * atr if side == "short" else zone - buf * atr
                risk = abs(sl - zone)
                if risk <= 0:
                    continue
                tp = zone - rr * risk if side == "short" else zone + rr * risk

                # 1. retest fill: forward bar taps the zone
                fwd = bars[i + 1:i + 1 + retest]
                fill_k = next((k for k, x in enumerate(fwd)
                               if (side == "short" and x.ohlc.h >= zone) or
                                  (side == "long" and x.ohlc.l <= zone)), None)
                if fill_k is None:
                    rows.append(dict(conf=r["strat_confirm"], vr=r["vol_ratio"],
                                     filled=False, R=0.0, exit="no_fill"))
                    continue

                # 2. manage from the fill bar
                mgmt = bars[i + 1 + fill_k: i + 1 + fill_k + MANAGE_N]
                R, ex = 0.0, "timeout"
                for x in mgmt:
                    hit_sl = (side == "short" and x.ohlc.h >= sl) or (side == "long" and x.ohlc.l <= sl)
                    hit_tp = (side == "short" and x.ohlc.l <= tp) or (side == "long" and x.ohlc.h >= tp)
                    if hit_sl:                       # SL first on same-bar tie
                        R, ex = -1.0, "sl"; break
                    if hit_tp:
                        R, ex = rr, "tp"; break
                else:
                    if mgmt:
                        last = mgmt[-1].ohlc.c
                        R = (zone - last) / risk if side == "short" else (last - zone) / risk
                rows.append(dict(conf=r["strat_confirm"], vr=r["vol_ratio"],
                                 filled=True, R=R, exit=ex))
    return rows


def summ(rs, tag):
    if not rs:
        print(f"{tag}: n=0"); return
    n = len(rs)
    filled = [r for r in rs if r["filled"]]
    fr = len(filled) / n
    Rall = [r["R"] for r in rs]              # unfilled = 0R (no trade)
    Rf = [r["R"] for r in filled]
    ex = {}
    for r in filled:
        ex[r["exit"]] = ex.get(r["exit"], 0) + 1
    print(f"\n=== {tag} (n={n}) ===")
    print(f"  fill-rate (retest tapped): {len(filled)}/{n} = {100*fr:.0f}%")
    if filled:
        print(f"  expectancy / FILLED trade: {st.mean(Rf):+.3f}R  "
              f"WR={100*sum(1 for x in Rf if x>0)/len(filled):.0f}%  sum={sum(Rf):+.1f}R")
    print(f"  expectancy / trigger (incl no-fill=0): {st.mean(Rall):+.3f}R")
    print(f"  exits: {ex}")


def main(argv):
    buf, retest, rr = DEFAULT_BUF, RETEST_N, DEFAULT_RR
    if "--buf" in argv: buf = float(argv[argv.index("--buf") + 1])
    if "--retest" in argv: retest = int(argv[argv.index("--retest") + 1])
    if "--rr" in argv: rr = float(argv[argv.index("--rr") + 1])
    print(f"params: SL_buf={buf}×ATR  retest_window={retest}  TP={rr}R")
    rows = run(buf, retest, rr)
    summ(rows, "ALL triggers")
    summ([r for r in rows if r["conf"]], "strat_confirm=True")
    summ([r for r in rows if r["vr"] >= 3.0], "climax vol>=3x")


if __name__ == "__main__":
    main(sys.argv[1:])
