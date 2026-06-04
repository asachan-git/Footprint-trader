#!/usr/bin/env python3
"""Deep-dive: CVD-divergence EXIT for reversal setups (15m + 5m).

Thesis (user): a reversal moves aggressively, then ranges/distributes at the
extreme — printing an OPPOSING divergence before it reverses back. So exit the
reversal trade on the first opposing CVD divergence (price extends but CVD fails)
rather than a fixed target.

Entry  : reversal trigger (absorption/climax winner) at the trigger-bar close.
SL     : entry ∓ 1.5×ATR.
Exit policies compared (per-bar sequencing, SL always checked first):
  fixed_2R   : TP = entry ± 2×risk
  cvd_div    : exit at the first FRESH opposing divergence (scan, incl EqH + live)
               after entry; else timeout at window end
  oracle_MFE : exit at forward MFE (ceiling)

    python scripts/validate_reversal_cvd_exit.py --tf 15m
    python scripts/validate_reversal_cvd_exit.py --tf 5m
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging; logging.disable(logging.CRITICAL)

import importlib.util
def _L(n, p):
    s = importlib.util.spec_from_file_location(n, ROOT / p); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
_obs = _L("absorption_observations", "scripts/absorption_observations.py")

from pipeline.features.cvd_candlestick import scan_divergences
from pipeline.features.atr import atr as _atr

MANAGE = 16
SL_ATR = 1.5


def run(tf):
    out = {}
    for sym in ("BTCUSDT", "XAUTUSDT"):
        bars = _obs.load_bars(sym, tf)
        if len(bars) < 60:
            continue
        im = {b.close_ts: i for i, b in enumerate(bars)}
        # all divergences once (no live — backtest), indexed by ts
        divs = scan_divergences(bars, lookback=3, include_live=False)
        bear_ts = sorted(d["ts"] for d in divs if d["type"] == "bear")
        bull_ts = sorted(d["ts"] for d in divs if d["type"] == "bull")
        rows = []
        for mode in ("momentum", "reversal"):
            for r in _obs.scan(sym, tf, mode):
                i = im.get(r["trigger_ts"])
                if i is None:
                    continue
                atr = _atr(bars[:i + 1]) or 0.0
                if atr <= 0:
                    continue
                side = r["winner"]; entry = bars[i].ohlc.c
                sl = entry - SL_ATR * atr if side == "long" else entry + SL_ATR * atr
                risk = SL_ATR * atr
                fwd = bars[i + 1:i + 1 + MANAGE]
                if len(fwd) < 3:
                    continue
                t0, t1 = bars[i].close_ts, fwd[-1].close_ts
                # opposing divergence after entry: long→bear, short→bull
                opp = bear_ts if side == "long" else bull_ts
                div_ts = next((t for t in opp if t0 < t <= t1), None)

                def R_at(price):
                    return (price - entry) / risk if side == "long" else (entry - price) / risk

                # fixed 2R
                tp = entry + 2 * risk if side == "long" else entry - 2 * risk
                fR = None
                for x in fwd:
                    hs = (side == "long" and x.ohlc.l <= sl) or (side == "short" and x.ohlc.h >= sl)
                    ht = (side == "long" and x.ohlc.h >= tp) or (side == "short" and x.ohlc.l <= tp)
                    if hs: fR = -1.0; break
                    if ht: fR = 2.0; break
                if fR is None: fR = R_at(fwd[-1].ohlc.c)

                # cvd-div exit: SL first, else exit at div bar close, else timeout
                dR = None; dexit = "timeout"
                for x in fwd:
                    if (side == "long" and x.ohlc.l <= sl) or (side == "short" and x.ohlc.h >= sl):
                        dR = -1.0; dexit = "sl"; break
                    if div_ts is not None and x.close_ts >= div_ts:
                        dR = R_at(x.ohlc.c); dexit = "div"; break
                if dR is None: dR = R_at(fwd[-1].ohlc.c)

                mfe_px = max(b.ohlc.h for b in fwd) if side == "long" else min(b.ohlc.l for b in fwd)
                rows.append(dict(conf=r["strat_confirm"], mode=mode,
                                 fixed=fR, div=dR, dexit=dexit, oracle=R_at(mfe_px),
                                 has_div=div_ts is not None))
        out[sym] = rows
    return out


def summ(rows, tag):
    if not rows: print(f"  {tag}: n=0"); return
    n = len(rows)
    def e(k): v = [r[k] for r in rows]; return f"{st.mean(v):+.3f}R (WR {100*sum(1 for x in v if x>0)/n:.0f}%)"
    dex = {}
    for r in rows: dex[r["dexit"]] = dex.get(r["dexit"], 0) + 1
    print(f"  {tag:20} n={n:3d} | fixed2R {e('fixed')} | cvd-div {e('div')} | oracle {e('oracle')} | exits {dex}")


def main(argv):
    tf = argv[argv.index("--tf") + 1] if "--tf" in argv else "15m"
    print(f"=== reversal CVD-div exit backtest — {tf} ===")
    data = run(tf)
    allrows = [r for rs in data.values() for r in rs]
    for sym, rows in data.items():
        summ(rows, sym)
    summ(allrows, f"ALL {tf}")
    summ([r for r in allrows if r["conf"]], f"ALL {tf} strat_confirm")
    summ([r for r in allrows if r["has_div"]], f"ALL {tf} (div fired)")


if __name__ == "__main__":
    main(sys.argv[1:])
