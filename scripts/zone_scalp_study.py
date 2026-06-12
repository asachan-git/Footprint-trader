#!/usr/bin/env python3
"""Zone-transition / scalp study — HVN↔LVN moves × CVD divergence × high-delta bias.

Tests the user's observations as ENTRY signals, measuring forward MFE/MAE (in
ATR) and a 1:1 scalp hit-rate (favorable target ATR reached before adverse stop
ATR within fwd_n bars), per timeframe (1m/5m/15m). Answers: is there a scalp edge
in any zone-transition, on any TF?

Observations under test:
  O3  LVN = continuation in the aggressor (delta) direction.            [signal O3_LVN]
  O4  Close inside an HVN → price drifts to an HVN edge.                [signal O4_HVN]
  O1  HVN→HVN: high-delta/vol break of an HVN edge continues to the     [signal O1_BRK]
      next HVN.
  O5  The move runs in the direction of recent high-delta candles.      [deltaBias filter]

Each signal is split by CVD-divergence alignment (+/−CVDdiv) and recent-high-delta-
bias alignment (+/−deltaBias) so the "extra confirmation" effect is visible.

Zones = HVN/LVN of the most-recent COMPLETED prior session (no lookahead), the
same convention as hvn2hvn_study.py. Reads bars from the state_store.

Output:
  data/reports/zone_scalp_study.md   sectioned aggregate (per TF × signal × filter)

Usage:
  .venv/bin/python scripts/zone_scalp_study.py
  .venv/bin/python scripts/zone_scalp_study.py --symbols BTCUSDT --tfs 15m 5m
"""
from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.state_store import store
from pipeline.features.atr import atr
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
from pipeline.features.cvd_candlestick import scan_divergences

REPORT = ROOT / "data" / "reports" / "zone_scalp_study.md"

CFG = {
    "1m":  {"vp_win": 1440, "fwd_n": 30},
    "5m":  {"vp_win": 288,  "fwd_n": 24},
    "15m": {"vp_win": 96,   "fwd_n": 16},
}
DELTA_HIGH = 0.35      # |delta|/vol ≥ → "high delta" candle
SCALP_TGT_ATR = 1.0    # favorable target (ATR) for the 1:1 scalp hit
SCALP_STOP_ATR = 1.0   # adverse stop (ATR)
RECENT_K = 10          # bars for the recent high-delta bias
SESS_MIN_BARS = 30
ATR_PERIOD = 14


def _sess_start_sec(sym): return 12600 if sym.startswith("XAU") else 19800
def _sess_key(ts, sym):   return (ts + 19800 - _sess_start_sec(sym)) // 86400


def _prior_session_zones(bars, symbol):
    """Per-bar-index → (hvn_zones, lvn_zones) of the most recent COMPLETED prior
    session. None until a prior session exists. No lookahead."""
    bin_size = DEFAULT_BIN_SIZE.get(symbol)
    groups = defaultdict(list)
    for idx, b in enumerate(bars):
        groups[_sess_key(b.close_ts, symbol)].append(idx)
    zby = {}
    for k in sorted(groups):
        seg = [bars[j] for j in groups[k]]
        if len(seg) < SESS_MIN_BARS:
            zby[k] = None
            continue
        try:
            vp = vp_compute(seg, "daily", seg[-1].ohlc.c, bin_size=bin_size)
            zby[k] = (vp.hvn_zones or [], vp.lvn_zones or [])
        except Exception:
            zby[k] = None
    prior, last = {}, None
    for k in sorted(groups):
        for j in groups[k]:
            prior[j] = last
        if zby.get(k) is not None:
            last = zby[k]
    return prior


def _zone_of(price, zones):
    for i, z in enumerate(zones):
        if z["low"] <= price <= z["high"]:
            return i
    return None


def _delta_ratio(b):
    tot = sum(l.vol for l in (b.bid_ladder or [])) + sum(l.vol for l in (b.ask_ladder or []))
    return ((b.delta or 0.0) / tot if tot > 0 else 0.0), tot


def _atr_at(bars, i):
    return atr(bars[max(0, i - ATR_PERIOD):i + 1]) or 0.0


def _fwd(bars, i, direction, atr_i, fwd_n):
    """Forward MFE/MAE (ATR) + 1:1 scalp hit (favorable target before adverse stop)."""
    if atr_i <= 0:
        return None
    entry = bars[i].ohlc.c
    mfe = mae = 0.0
    hit = None
    for b in bars[i + 1:i + 1 + fwd_n]:
        fav = (b.ohlc.h - entry) if direction > 0 else (entry - b.ohlc.l)
        adv = (entry - b.ohlc.l) if direction > 0 else (b.ohlc.h - entry)
        mfe = max(mfe, fav / atr_i)
        mae = max(mae, adv / atr_i)
        if hit is None:
            if fav >= SCALP_TGT_ATR * atr_i:
                hit = "tgt"
            elif adv >= SCALP_STOP_ATR * atr_i:
                hit = "stop"
    return {"mfe": mfe, "mae": mae, "hit": hit}


def _recent_delta_bias(bars, i):
    s = sum((b.delta or 0.0) for b in bars[max(0, i - RECENT_K):i]
            if abs(_delta_ratio(b)[0]) >= DELTA_HIGH)
    return 0 if s == 0 else (1 if s > 0 else -1)


def _causal_div_aligned(bars, i, direction):
    """CAUSAL CVD-divergence check: the LIVE provisional marker derived only from
    bars[:i+1] (exactly what the live strategy sees) — no future bars. Avoids the
    lookahead of precomputed confirmed-pivot divs (whose pivot needs `lookback`
    FUTURE bars to confirm). bull div → supports long, bear → short."""
    want = "bull" if direction > 0 else "bear"
    divs = scan_divergences(bars[max(0, i - 120):i + 1], lookback=3, include_live=True)
    return any(d.get("live") and d["type"] == want for d in divs)


def run_symbol_tf(symbol, tf):
    bars = list(store()._bars.get((symbol, tf), []))
    n = len(bars)
    if n < 300:
        return None
    fwd_n = CFG[tf]["fwd_n"]
    prior = _prior_session_zones(bars, symbol)

    rec = defaultdict(lambda: defaultdict(list))

    def add(sig, i, direction):
        atr_i = _atr_at(bars, i)
        f = _fwd(bars, i, direction, atr_i, fwd_n)
        if f is None:
            return
        bias = _recent_delta_bias(bars, i)
        div = _causal_div_aligned(bars, i, direction)   # causal — no lookahead
        rec[sig]["ALL"].append(f)
        rec[sig]["+CVDdiv" if div else "−CVDdiv"].append(f)
        rec[sig]["+deltaBias" if bias == direction else "−deltaBias"].append(f)

    for i in range(ATR_PERIOD + 1, n - fwd_n):
        zp = prior.get(i)
        if zp is None:
            continue
        hvns, lvns = zp
        c = bars[i].ohlc.c
        dr, _ = _delta_ratio(bars[i])
        d_sign = 0 if not bars[i].delta else (1 if bars[i].delta > 0 else -1)

        # O3 — LVN continuation in the aggressor (delta) direction
        if _zone_of(c, lvns) is not None and abs(dr) >= DELTA_HIGH and d_sign:
            add("O3_LVN_continuation", i, d_sign)

        # O4 — close inside an HVN → drift to an HVN edge (delta direction)
        hi = _zone_of(c, hvns)
        if hi is not None and d_sign:
            add("O4_HVN_to_edge", i, d_sign)

        # O1 — HVN→HVN: high-delta/vol break of an HVN edge toward an adjacent HVN
        if hi is None and abs(dr) >= DELTA_HIGH and d_sign and hvns:
            prev_in = _zone_of(bars[i - 1].ohlc.c, hvns)
            if prev_in is not None:
                z = hvns[prev_in]
                broke = (d_sign > 0 and c > z["high"]) or (d_sign < 0 and c < z["low"])
                nxt = [zz for zz in hvns if zz is not z and
                       ((zz["low"] > z["high"]) if d_sign > 0 else (zz["high"] < z["low"]))]
                if broke and nxt:
                    add("O1_HVN_break_to_HVN", i, d_sign)
    return {"n_bars": n, "rec": rec}


def _stats(fwds):
    n = len(fwds)
    if not n:
        return None
    hits = sum(1 for f in fwds if f["hit"] == "tgt")
    stops = sum(1 for f in fwds if f["hit"] == "stop")
    resolved = hits + stops
    return {
        "n": n,
        "wr": (hits / resolved) if resolved else float("nan"),
        "res": resolved / n,
        "mfe": statistics.median(f["mfe"] for f in fwds),
        "mae": statistics.median(f["mae"] for f in fwds),
        "expR": (hits - stops) / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "XAUTUSDT"])
    ap.add_argument("--tfs", nargs="+", default=["15m", "5m", "1m"])
    args = ap.parse_args()

    lines = ["# Zone-transition / scalp study", "",
             f"Scalp = {SCALP_TGT_ATR}:{SCALP_STOP_ATR} ATR within fwd_n bars; "
             f"high-delta = |Δ|/vol ≥ {DELTA_HIGH}; zones = prior completed session VP. "
             f"expR = (tgt−stop)/n (net R/signal at 1:1). CVD div is CAUSAL (live "
             f"marker from bars[:i+1] — no lookahead). NOTE: signals overlap (per-bar, "
             f"not independent trades); 1:1 ATR ignores fees/spread/slippage.", ""]
    for sym in args.symbols:
        lines.append(f"\n## {sym}")
        for tf in args.tfs:
            res = run_symbol_tf(sym, tf)
            if res is None:
                lines.append(f"\n### {tf} — insufficient bars"); continue
            lines.append(f"\n### {tf}  ({res['n_bars']} bars, fwd_n={CFG[tf]['fwd_n']})")
            lines.append(f"{'signal/filter':<30}{'n':>6}{'scalpWR':>9}{'res%':>7}{'medMFE':>8}{'medMAE':>8}{'expR':>8}")
            for sig in ("O3_LVN_continuation", "O4_HVN_to_edge", "O1_HVN_break_to_HVN"):
                for filt in ("ALL", "+CVDdiv", "−CVDdiv", "+deltaBias", "−deltaBias"):
                    st = _stats(res["rec"][sig][filt])
                    if not st:
                        continue
                    wr = f"{100*st['wr']:.0f}%" if st['wr'] == st['wr'] else "n/a"
                    lines.append(f"{(sig+'/'+filt):<30}{st['n']:>6}{wr:>9}{100*st['res']:>6.0f}%"
                                 f"{st['mfe']:>8.2f}{st['mae']:>8.2f}{st['expR']:>+8.2f}")
            lines.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\n[written] {REPORT}")


if __name__ == "__main__":
    main()
