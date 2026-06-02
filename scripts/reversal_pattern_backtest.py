#!/usr/bin/env python3
"""Backtest the derived reversal pattern (from reversal_study.py §6/§7).

Pattern (causal — every input known at the entry bar, no future leak):
  1. bar i is a swing pivot: a local high over [i-L .. i] AND strictly above the
     next bar i+1 (top); mirror for a low. Confirmed at i+1.
  2. CLIMAX: pivot volume >= VOL_MULT x median of the prior L bars.
  3. FLIP at i+1: the candle closes the reversal way AND the reversal-aligned
     delta swings up by >= DELTA_SWING vs the pivot bar.
Enter at the i+1 close, reversal direction (top->short, bottom->long). SL just
beyond the pivot extreme (+ buffer). TP = RR x risk. First-touch SL/TP, time-stop.

Sweeps VOL_MULT x DELTA_SWING; reports per (symbol, tf) and combined.

Usage: .venv/bin/python scripts/reversal_pattern_backtest.py
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
from pipeline.features.atr import atr
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE
from scripts.reversal_study import load_bars

VP_WIN = {"15m": 96, "5m": 288}   # ~24h trailing window for the VP context

SYMBOLS = ["BTCUSDT", "XAUTUSDT"]
TFS = ["15m", "5m"]
LEFT_L = 3            # left-context bars for the causal pivot
VOL_LB = 20          # trailing-median window for the climax ratio
RR = 2.0             # take-profit in R
MAX_HOLD = 24        # bars before time-stop
ATR_PERIOD = 14
SL_BUF_ATR = 0.10    # SL buffer beyond the pivot extreme, in ATR

VOL_MULTS = [1.5, 2.0, 2.5]
DELTA_SWINGS = [0, 50, 80]


def _vol(bar) -> float:
    fp = build_fp(bar)
    return fp.total_bid + fp.total_ask


def _trend(bars, i, lb=50) -> str:
    """Trend at bar i from a SMA-lb of closes: price-vs-SMA + SMA slope."""
    if i < lb:
        return "range"
    closes = [b.ohlc.c for b in bars[i - lb + 1:i + 1]]
    sma_now = sum(closes) / len(closes)
    prev = [b.ohlc.c for b in bars[i - lb:i]]
    sma_prev = sum(prev) / len(prev)
    c = bars[i].ohlc.c
    if c > sma_now and sma_now > sma_prev:
        return "bull"
    if c < sma_now and sma_now < sma_prev:
        return "bear"
    return "range"


def _vp_ctx(bars, i, symbol, tf, price, side, atr_val):
    """VP context at the pivot: nearest level, distance in ATR, VA position, and
    whether the reversal fades a value-area extreme (long at/below VAL, short
    at/above VAH) — the classic 'reversals hold at value edges' read."""
    win = VP_WIN.get(tf, 96)
    if i < win:
        return {"near_level": "none", "dist_atr": None, "vp_pos": "unknown", "va_aligned": False}
    vp = vp_compute(bars[i - win + 1:i + 1], "daily", bars[i].ohlc.c,
                    bin_size=DEFAULT_BIN_SIZE.get(symbol))
    cands = []
    for lab, v in (("POC", vp.poc), ("VAH", vp.vah), ("VAL", vp.val)):
        if v is not None:
            cands.append((lab, v))
    for z in (vp.hvn_zones or []):
        cands.append(("HVN", (z["low"] + z["high"]) / 2))
    for z in (vp.lvn_zones or []):
        cands.append(("LVN", (z["low"] + z["high"]) / 2))
    near_level, dist_atr = "none", None
    if cands:
        lab, lvl = min(cands, key=lambda c: abs(price - c[1]))
        near_level = lab
        dist_atr = abs(price - lvl) / atr_val if atr_val > 0 else None
    # fade a value-area extreme?  long at a bottom should sit at/below VAL; short at top at/above VAH
    va_aligned = False
    if side == "long" and vp.val is not None:
        va_aligned = price <= vp.val
    elif side == "short" and vp.vah is not None:
        va_aligned = price >= vp.vah
    return {"near_level": near_level, "dist_atr": dist_atr,
            "vp_pos": vp.current_position, "va_aligned": va_aligned}


def _imbalance_sl(piv, trade_side, buf):
    """SL beyond the trapped-side stacked-imbalance zone on the pivot bar.
    long → sellers trapped at the low → SL below the strongest SELL zone;
    short → buyers trapped at the high → SL above the strongest BUY zone.
    Returns None if no zone (caller falls back to the candle extreme)."""
    from pipeline.features.stacked_imbalance import stacked_imbalances
    trapped = "sell" if trade_side == "long" else "buy"
    zones = [z for z in stacked_imbalances(build_fp(piv), min_stack=3) if z.side == trapped]
    if not zones:
        return None
    z = max(zones, key=lambda z: z.count)
    return (z.price_low - buf) if trade_side == "long" else (z.price_high + buf)


def _entry_level(piv, trade_side, mode):
    """Footprint LIMIT entry level on the pivot bar (None → can't place).
      imbalance — near edge of the trapped-side stacked-imbalance zone: long pulls
                  back DOWN into the SELL cluster (its high); short up into the BUY
                  cluster (its low).
      lvn       — low-volume bin between the pivot extreme and POC (retrace vacuum)."""
    from pipeline.features.stacked_imbalance import stacked_imbalances
    fp = build_fp(piv)
    if mode == "imbalance":
        trapped = "sell" if trade_side == "long" else "buy"
        zones = [z for z in stacked_imbalances(fp, min_stack=3) if z.side == trapped]
        if not zones:
            return None
        z = max(zones, key=lambda z: z.count)
        return z.price_high if trade_side == "long" else z.price_low
    if mode == "lvn":
        if not fp.cells or fp.poc_price is None:
            return None
        poc = fp.poc_price
        region = [c for c in fp.cells
                  if c.total > 0 and (c.price <= poc if trade_side == "long" else c.price >= poc)]
        if len(region) < 2:
            return None
        return min(region, key=lambda c: c.total).price
    return None


FILL_WITHIN = 3   # bars after the flip a limit entry has to be touched, else void


def _sim(side, entry, sl, tp, future):
    """First-touch SL/TP then time-stop. Return (R, reason) or None."""
    risk = abs(entry - sl) or 1e-9
    for k, b in enumerate(future):
        if k >= MAX_HOLD:
            r = ((b.ohlc.c - entry) if side == "long" else (entry - b.ohlc.c)) / risk
            return r, "hold"
        if side == "long":
            if b.ohlc.l <= sl: return -abs(entry - sl) / risk, "sl"
            if b.ohlc.h >= tp: return abs(tp - entry) / risk, "tp"
        else:
            if b.ohlc.h >= sl: return -abs(sl - entry) / risk, "sl"
            if b.ohlc.l <= tp: return abs(entry - tp) / risk, "tp"
    return None


def signals(bars, vol_mult, delta_swing, rr=RR, sl_mode="pivot", sl_atr=1.0,
            symbol="BTCUSDT", tf="15m"):
    """Yield trades matching the pattern. Entry at i+1 close.
    sl_mode: 'pivot' = beyond the pivot extreme (+buf); 'atr' = entry ± sl_atr×ATR."""
    out = []
    n = len(bars)
    open_until = -1
    for i in range(LEFT_L + VOL_LB, n - 2):
        if i <= open_until:
            continue
        piv, nxt = bars[i], bars[i + 1]
        # ── causal pivot (left window + 1 right bar) ──
        left_h = [b.ohlc.h for b in bars[i - LEFT_L:i]]
        left_l = [b.ohlc.l for b in bars[i - LEFT_L:i]]
        is_top = piv.ohlc.h >= max(left_h) and piv.ohlc.h > nxt.ohlc.h
        is_bot = piv.ohlc.l <= min(left_l) and piv.ohlc.l < nxt.ohlc.l
        if not (is_top or is_bot):
            continue
        side = "high" if is_top else "low"
        # ── climax ──
        prior = [_vol(b) for b in bars[i - VOL_LB:i] if _vol(b) > 0]
        if not prior:
            continue
        vr = _vol(piv) / statistics.median(prior)
        if vr < vol_mult:
            continue
        # ── flip at i+1 (reversal-aligned) ──
        pd = build_fp(piv).delta
        nd = build_fp(nxt).delta
        rev_piv = -pd if is_top else pd
        rev_nxt = -nd if is_top else nd
        closed_rev = (nxt.ohlc.c < nxt.ohlc.o) if is_top else (nxt.ohlc.c > nxt.ohlc.o)
        if not (closed_rev and rev_nxt > 0 and (rev_nxt - rev_piv) >= delta_swing):
            continue
        # ── trade ──
        trade_side = "short" if is_top else "long"
        entry = float(nxt.ohlc.c)
        a = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD)
        buf = SL_BUF_ATR * a
        candle_sl = (max(piv.ohlc.h, nxt.ohlc.h) + buf) if is_top else (min(piv.ohlc.l, nxt.ohlc.l) - buf)
        if sl_mode == "atr":
            sl = entry - sl_atr * a if trade_side == "long" else entry + sl_atr * a
        elif sl_mode == "imbalance":
            sl = _imbalance_sl(piv, trade_side, buf)
            if sl is None:
                sl = candle_sl
            # SL must be on the correct side of entry (imbalance zone can sit inside)
            if (trade_side == "long" and sl >= entry) or (trade_side == "short" and sl <= entry):
                sl = candle_sl
        else:
            sl = candle_sl
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + rr * risk if trade_side == "long" else entry - rr * risk
        r = _sim(trade_side, entry, sl, tp, bars[i + 2:])
        if r is None:
            continue
        out.append({"i": i, "side": trade_side, "r": r[0], "reason": r[1], "vr": round(vr, 2),
                    "trend": _trend(bars, i),
                    **_vp_ctx(bars, i, symbol, tf, pivot_price := (piv.ohlc.h if is_top else piv.ohlc.l),
                              trade_side, a)})
        # one trade at a time: block re-entry until this one would have closed
        open_until = i + 1 + MAX_HOLD
    return out


def summarize(trades):
    if not trades:
        return "n=0"
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    longs = sum(1 for t in trades if t["side"] == "long")
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    pf = gains / losses if losses > 0 else float("inf")
    return (f"n={len(rs):3d}  WR={100*wins/len(rs):3.0f}%  sumR={sum(rs):+6.2f}  "
            f"avgR={statistics.mean(rs):+.3f}  PF={pf:4.2f}  L/S={longs}/{len(rs)-longs}")


def entry_ab(bars, vol_mult, delta_swing, symbol, tf, entry_mode, vp_only=True):
    """Re-run the pattern with a chosen ENTRY mode; SL = trapped-side imbalance far
    edge, TP = 2R from the ACTUAL fill. Returns (trades, attempts) so the caller can
    report fill-rate. market = enter at flip close (always fills); imbalance/lvn =
    LIMIT, filled only if touched within FILL_WITHIN bars (else voided)."""
    trades, attempts = [], 0
    n = len(bars)
    open_until = -1
    for i in range(LEFT_L + VOL_LB, n - 2):
        if i <= open_until:
            continue
        piv, nxt = bars[i], bars[i + 1]
        left_h = [b.ohlc.h for b in bars[i - LEFT_L:i]]
        left_l = [b.ohlc.l for b in bars[i - LEFT_L:i]]
        is_top = piv.ohlc.h >= max(left_h) and piv.ohlc.h > nxt.ohlc.h
        is_bot = piv.ohlc.l <= min(left_l) and piv.ohlc.l < nxt.ohlc.l
        if not (is_top or is_bot):
            continue
        prior = [_vol(b) for b in bars[i - VOL_LB:i] if _vol(b) > 0]
        if not prior:
            continue
        vr = _vol(piv) / statistics.median(prior)
        if vr < vol_mult:
            continue
        pd, nd = build_fp(piv).delta, build_fp(nxt).delta
        rev_piv = -pd if is_top else pd
        rev_nxt = -nd if is_top else nd
        closed_rev = (nxt.ohlc.c < nxt.ohlc.o) if is_top else (nxt.ohlc.c > nxt.ohlc.o)
        if not (closed_rev and rev_nxt > 0 and (rev_nxt - rev_piv) >= delta_swing):
            continue
        side = "short" if is_top else "long"
        a = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD)
        ctx = _vp_ctx(bars, i, symbol, tf, (piv.ohlc.h if is_top else piv.ohlc.l), side, a)
        if vp_only and not (ctx["va_aligned"] and ctx["near_level"] != "HVN"):
            continue

        attempts += 1
        buf = SL_BUF_ATR * a
        # find entry + the bar index where the position is actually live
        if entry_mode == "market":
            entry = float(nxt.ohlc.c)
            fut = bars[i + 2:]
        else:
            lvl = _entry_level(piv, side, entry_mode)
            if lvl is None:
                continue
            fk = None
            for k, b in enumerate(bars[i + 2:i + 2 + FILL_WITHIN]):
                if b.ohlc.l <= lvl <= b.ohlc.h:
                    fk = k
                    break
            if fk is None:
                continue                      # limit never touched → voided
            entry = float(lvl)
            fut = bars[i + 2 + fk + 1:]        # walk from the bar after the fill
        # SL = trapped-side imbalance far edge (structural), fallback candle extreme
        sl = _imbalance_sl(piv, side, buf)
        candle_sl = (max(piv.ohlc.h, nxt.ohlc.h) + buf) if is_top else (min(piv.ohlc.l, nxt.ohlc.l) - buf)
        if sl is None or (side == "long" and sl >= entry) or (side == "short" and sl <= entry):
            sl = candle_sl
        risk = abs(entry - sl)
        if risk <= 0 or not fut:
            continue
        tp = entry + RR * risk if side == "long" else entry - RR * risk
        r = _sim(side, entry, sl, tp, fut)
        if r is None:
            continue
        trades.append({"i": i, "side": side, "r": r[0], "reason": r[1], **ctx})
        open_until = i + 1 + MAX_HOLD
    return trades, attempts


def _with_trend(t):
    """A reversal aligned WITH the larger trend = buy-the-dip in a bull, sell-the-
    rip in a bear. Counter-trend = long in a bear / short in a bull (the knife-catch)."""
    return (t["trend"] == "bull" and t["side"] == "long") or \
           (t["trend"] == "bear" and t["side"] == "short")


def _counter_trend(t):
    return (t["trend"] == "bear" and t["side"] == "long") or \
           (t["trend"] == "bull" and t["side"] == "short")


def main():
    data = {(s, tf): load_bars(s, tf) for s in SYMBOLS for tf in TFS}
    VM, DS = 2.0, 50   # fixed filter for the analysis (climax + flip)

    def run(sl_mode="pivot"):
        out = []
        for (s, tf), bars in data.items():
            out += signals(bars, VM, DS, sl_mode=sl_mode, symbol=s, tf=tf)
        return out

    allt = run()

    print(f"# Reversal-pattern analysis (vol×≥{VM}, Δswing≥{DS}, SL=candle, 2R)\n")
    print(f"ALL  {summarize(allt)}\n")

    # ── 1. SL-hitters: trend × side ──
    print("=== 1. Where do SL-hitters come from? (outcome by trend × side) ===")
    print("| trend | side | n | SL-hit% | sumR | avgR |")
    print("|---|---|---|---|---|---|")
    for tr in ("bull", "bear", "range"):
        for sd in ("long", "short"):
            sub = [t for t in allt if t["trend"] == tr and t["side"] == sd]
            if not sub:
                continue
            sl = sum(1 for t in sub if t["reason"] == "sl")
            print(f"| {tr} | {sd} | {len(sub)} | {100*sl/len(sub):.0f}% | "
                  f"{sum(t['r'] for t in sub):+.2f} | {statistics.mean(t['r'] for t in sub):+.3f} |")

    # ── 2. VP filters: does volume-profile context separate winners? ──
    print("\n=== 2. VP context filters (on ALL trades, no trend filter) ===")
    def show(name, sub):
        print(f"  {name:28s} {summarize(sub)}")
    show("ALL (baseline)", allt)
    show("near a VP level ≤0.5ATR", [t for t in allt if (t['dist_atr'] or 9) <= 0.5])
    show("near a VP level ≤1.0ATR", [t for t in allt if (t['dist_atr'] or 9) <= 1.0])
    show("far from levels >1.0ATR", [t for t in allt if (t['dist_atr'] or 9) > 1.0])
    print()
    show("VA-extreme aligned (fade)", [t for t in allt if t['va_aligned']])
    show("NOT VA-aligned", [t for t in allt if not t['va_aligned']])

    # ── 3. by nearest VP level type ──
    print("\n=== 3. By nearest VP level type ===")
    for lv in ("POC", "VAH", "VAL", "HVN", "LVN", "none"):
        sub = [t for t in allt if t['near_level'] == lv]
        if sub:
            show(lv, sub)

    # ── 4. by VA position (price location vs value area) ──
    print("\n=== 4. By VA position at the pivot ===")
    poss = sorted({t['vp_pos'] for t in allt})
    for p in poss:
        show(p, [t for t in allt if t['vp_pos'] == p])

    # ── 5. best combo: VA-extreme fade + imbalance SL ──
    print("\n=== 5. Combo: VA-extreme aligned + imbalance SL (drop counter-trend too) ===")
    imb = run("imbalance")
    show("VA-aligned only", [t for t in imb if t['va_aligned']])
    show("VA-aligned + ¬counter-trend", [t for t in imb if t['va_aligned'] and not _counter_trend(t)])

    # ── 6. ENTRY-MODE A/B (live setup: VP-gated, imbalance SL, 2R) ──
    print("\n=== 6. Entry-mode A/B (VP-gated, imbalance SL, 2R) — fill-rate matters ===")
    for em in ("market", "imbalance", "lvn"):
        allt, att = [], 0
        for (s, tf), bars in data.items():
            t, a = entry_ab(bars, VM, DS, s, tf, em, vp_only=True)
            allt += t
            att += a
        fr = f"{100*len(allt)/att:.0f}%" if att else "—"
        print(f"  entry={em:10s} fills={len(allt)}/{att} ({fr})  {summarize(allt)}")


if __name__ == "__main__":
    main()
