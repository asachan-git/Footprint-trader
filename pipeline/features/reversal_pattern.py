"""Reversal-pattern detector (from the reversal_study findings).

Marks the candidate "climax pivot → next-bar delta flip" reversal pattern for
chart inspection. CAUSAL: a pivot bar i is confirmed at i+1, which is exactly the
flip bar — every input is known when the marker prints (no future leak).

Pattern at bar i (pivot) + i+1 (flip):
  1. PIVOT: bar i is a local high over [i-LEFT_L .. i] and strictly above i+1
     (top → short); mirror for a low (bottom → long).
  2. CLIMAX: pivot volume >= vol_mult × median of the prior LEFT-window volumes.
  3. FLIP at i+1: closes the reversal way AND the reversal-aligned delta swings up
     by >= delta_swing vs the pivot bar.

NOTE: backtested as a 2R entry this is ~breakeven (only BTC 15m holds a thin edge)
— this is a VISUAL/diagnostic overlay, not a trade signal. See
scripts/reversal_pattern_backtest.py.
"""
from __future__ import annotations

import statistics

from pipeline.footprint import build as build_fp
from pipeline.features.atr import atr
from pipeline.features.stacked_imbalance import stacked_imbalances
from pipeline.features.volume_profile import compute as vp_compute, DEFAULT_BIN_SIZE

LEFT_L = 3
VOL_LB = 20
ATR_PERIOD = 14
SL_BUF_ATR = 0.10
RR = 2.0
VP_WIN = {"15m": 96, "5m": 288, "1m": 1440}   # ~24h trailing VP window


def _vol(bar) -> float:
    fp = build_fp(bar)
    return fp.total_bid + fp.total_ask


def _imbalance_sl(piv, side, buf):
    """SL beyond the trapped-side stacked-imbalance zone on the pivot bar.
    long → sellers trapped at the low → SL below the strongest SELL zone;
    short → buyers trapped at the high → SL above the strongest BUY zone.
    Returns None if no qualifying zone."""
    trapped = "sell" if side == "long" else "buy"
    zones = [z for z in stacked_imbalances(build_fp(piv), min_stack=3) if z.side == trapped]
    if not zones:
        return None
    z = max(zones, key=lambda z: z.count)
    return (z.price_low - buf) if side == "long" else (z.price_high + buf)


def _vp_gate(bars, i, symbol, tf, price, side):
    """VP filter (backtest §2-5): keep reversals that FADE a value-area extreme
    (long at/below VAL, short at/above VAH) and are NOT nearest a HVN magnet
    (HVN reversals had PF 0.43). Returns (pass: bool, near_level, vp_pos)."""
    win = VP_WIN.get(tf, 96)
    if i < win:
        return True, "none", "unknown"        # not enough history → don't gate
    vp = vp_compute(bars[i - win + 1:i + 1], "daily", bars[i].ohlc.c,
                    bin_size=DEFAULT_BIN_SIZE.get(symbol))
    # nearest level type
    cands = []
    for lab, v in (("POC", vp.poc), ("VAH", vp.vah), ("VAL", vp.val)):
        if v is not None:
            cands.append((lab, v))
    for z in (vp.hvn_zones or []):
        cands.append(("HVN", (z["low"] + z["high"]) / 2))
    for z in (vp.lvn_zones or []):
        cands.append(("LVN", (z["low"] + z["high"]) / 2))
    near = min(cands, key=lambda c: abs(price - c[1]))[0] if cands else "none"
    va_aligned = ((side == "long" and vp.val is not None and price <= vp.val) or
                  (side == "short" and vp.vah is not None and price >= vp.vah))
    return (va_aligned and near != "HVN"), near, vp.current_position


def detect(bars: list, vol_mult: float = 2.0, delta_swing: float = 50.0,
           symbol: str = "BTCUSDT", tf: str = "15m", vp_filter: bool = True) -> list[dict]:
    """Return reversal-pattern markers across the history.

    Each: {ts (flip bar), pivot_ts, side ("long"|"short"), pivot_price, entry,
    sl, tp, vol_ratio, delta_swing, sl_basis, near_level, vp_pos}. ts is the flip
    (i+1) bar — when the pattern is knowable and where entry would print.

    vp_filter (default on): keep only reversals fading a value-area extreme and not
    at a HVN magnet (backtest: that subset + imbalance SL = PF 1.50 vs 0.95 baseline).
    """
    out: list[dict] = []
    n = len(bars)
    if n < LEFT_L + VOL_LB + 3:
        return out

    for i in range(LEFT_L + VOL_LB, n - 1):
        piv, nxt = bars[i], bars[i + 1]
        left_h = [b.ohlc.h for b in bars[i - LEFT_L:i]]
        left_l = [b.ohlc.l for b in bars[i - LEFT_L:i]]
        if not left_h:
            continue
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

        pd = build_fp(piv).delta
        nd = build_fp(nxt).delta
        rev_piv = -pd if is_top else pd
        rev_nxt = -nd if is_top else nd
        closed_rev = (nxt.ohlc.c < nxt.ohlc.o) if is_top else (nxt.ohlc.c > nxt.ohlc.o)
        swing = rev_nxt - rev_piv
        if not (closed_rev and rev_nxt > 0 and swing >= delta_swing):
            continue

        a = atr(bars[max(0, i - 50):i + 1], ATR_PERIOD)
        buf = SL_BUF_ATR * a
        entry = float(nxt.ohlc.c)
        side = "short" if is_top else "long"
        pivot_price = piv.ohlc.h if is_top else piv.ohlc.l
        # ── VP gate: fade value-area extremes, skip HVN magnets ──
        vp_ok, near_level, vp_pos = _vp_gate(bars, i, symbol, tf, pivot_price, side)
        if vp_filter and not vp_ok:
            continue
        # SL: prefer the trapped-side stacked-imbalance (big-trade) zone edge; fall
        # back to the structural swing high/low + ATR buffer when no zone qualifies.
        candle_sl = (max(piv.ohlc.h, nxt.ohlc.h) + buf) if is_top else (min(piv.ohlc.l, nxt.ohlc.l) - buf)
        sl = _imbalance_sl(piv, side, buf)
        sl_basis = "imbalance"
        # imbalance zone can sit inside the candle → must stay beyond entry
        if sl is None or (side == "long" and sl >= entry) or (side == "short" and sl <= entry):
            sl = candle_sl
            sl_basis = "swing_high" if is_top else "swing_low"
        tp = entry + RR * abs(entry - sl) if side == "long" else entry - RR * abs(entry - sl)

        out.append({
            "ts": nxt.close_ts,
            "pivot_ts": piv.close_ts,
            "side": side,
            "pivot_price": round(pivot_price, 4),
            "entry": round(entry, 4),
            "sl": round(sl, 4),
            "tp": round(tp, 4),
            "vol_ratio": round(vr, 2),
            "delta_swing": round(swing, 1),
            "sl_basis": sl_basis,
            "near_level": near_level,
            "vp_pos": vp_pos,
        })
    return out
