"""Per-candle Binance(analysis)-vs-Vantage(venue) feed discrepancy check.

The system trades on two price streams — the Binance analysis feed (draws HVN/LVN zones +
ATR) and the Vantage venue (fills orders). They differ by a STEADY ~3pt gold basis, handled
by the additive zone_shift rebase. This module verifies the two stay consistent bar-to-bar:
it flags a candle only when the venue−analysis delta DEVIATES from the expected (rolling)
basis beyond tolerance — a raw close-vs-close compare would flag every candle because of the
legitimate basis.

Two independent breach signals (either trips):
  1. basis residual  — (venue_c − analysis_c) deviates from the rolling-median basis
  2. return divergence — the bar's own close-to-close move differs between the two feeds
     (catches a frozen/stale analysis candle whose absolute price still looks in-basis)

Diagnostic + heal only — never touches arming/exits (those are guarded elsewhere).
Complements the gap auto-backfill (heals MISSING bars); this catches WRONG bars.
"""

from __future__ import annotations

import statistics


def _closes(bars: list) -> list[float]:
    out = []
    for b in bars:
        c = getattr(getattr(b, "ohlc", None), "c", None)
        if c is not None:
            out.append(float(c))
    return out


def rolling_basis(venue_bars: list, analysis_bars: list, window: int) -> float | None:
    """Median of (venue_close − analysis_close) over the last `window` bars that share a
    close_ts. Robust to a single bad candle (median, not mean). None if too few shared bars."""
    a_by_ts = {int(getattr(b, "close_ts", 0)): float(b.ohlc.c)
               for b in analysis_bars if getattr(b, "close_ts", 0) and b.ohlc.c}
    diffs: list[float] = []
    for vb in venue_bars:
        ts = int(getattr(vb, "close_ts", 0) or 0)
        ac = a_by_ts.get(ts)
        if ac is not None and getattr(vb.ohlc, "c", None):
            diffs.append(float(vb.ohlc.c) - ac)
    diffs = diffs[-window:]
    if len(diffs) < 3:
        return None
    return statistics.median(diffs)


def compare_close(analysis_symbol: str, tf: str, venue_bars: list, analysis_bars: list,
                  tol_pct: float, tol_abs: float, basis_window: int = 12) -> dict | None:
    """Compare the latest CLOSED venue candle against the analysis candle at the same close_ts,
    basis-adjusted. Returns a breach dict if either signal exceeds tolerance, else None.

    venue_bars / analysis_bars: ascending (oldest→newest) bars for THIS tf; the newest of each
    is the just-closed candle. Uses the shared-close history for the rolling basis + the prior
    shared close for the return-divergence signal. Returns None (no breach) when data is
    insufficient — fail-safe, a missing reference is not a discrepancy."""
    if not venue_bars or not analysis_bars:
        return None
    vb = venue_bars[-1]
    v_ts = int(getattr(vb, "close_ts", 0) or 0)
    v_c = float(getattr(vb.ohlc, "c", 0.0) or 0.0)
    if v_ts <= 0 or v_c <= 0:
        return None

    # Match the analysis bar at the SAME close_ts (as-of exact).
    a_by_ts = {int(getattr(b, "close_ts", 0)): b
               for b in analysis_bars if getattr(b, "close_ts", 0)}
    ab = a_by_ts.get(v_ts)
    if ab is None or not getattr(ab.ohlc, "c", None):
        return None   # no matching analysis candle yet (feed lag) — the stale gate owns this
    a_c = float(ab.ohlc.c)

    # Rolling basis from PRIOR shared closes (exclude the candle under test so a bad current
    # candle can't pull the basis toward itself).
    basis = rolling_basis(venue_bars[:-1], analysis_bars, basis_window)
    if basis is None:
        return None   # not enough history to know the basis yet

    tol = max(float(tol_abs), float(tol_pct) * v_c)

    # Signal 1: basis residual.
    resid = (v_c - a_c) - basis

    # Signal 2: return divergence — compare each feed's own close-to-close move over the last
    # shared step (basis-independent, so a frozen analysis close vs a moving venue close trips
    # it even when the absolute price still looks in-basis).
    ret_div = 0.0
    prev_ts = None
    v_by_ts = {int(getattr(b, "close_ts", 0)): float(b.ohlc.c)
               for b in venue_bars if getattr(b, "close_ts", 0) and b.ohlc.c}
    shared_prev = sorted(t for t in (set(v_by_ts) & set(a_by_ts)) if t < v_ts)
    if shared_prev:
        prev_ts = shared_prev[-1]
        v_ret = v_c - v_by_ts[prev_ts]
        a_ret = a_c - float(a_by_ts[prev_ts].ohlc.c)
        ret_div = v_ret - a_ret

    breach_resid = abs(resid) > tol
    breach_return = abs(ret_div) > tol
    if not (breach_resid or breach_return):
        return None

    return {
        "symbol": analysis_symbol, "tf": tf, "close_ts": v_ts,
        "venue_c": round(v_c, 4), "analysis_c": round(a_c, 4),
        "basis": round(basis, 4), "resid": round(resid, 4), "ret_div": round(ret_div, 4),
        "tol": round(tol, 4), "prev_ts": prev_ts,
        "signal": "basis" if breach_resid else "return",
    }
