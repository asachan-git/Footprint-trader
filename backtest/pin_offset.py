"""Pin the "auto" venue offset to a fixed number for replay.

settings.yaml sets XAUTUSDT venue_price_offset: "auto" — resolved live from an MT5
quote at build time, which is unreproducible historically. This computes a fixed
offset (Vantage − Binance) empirically and reports its dispersion, so the harness
uses a real number and SURFACES how much the basis wanders (a wander wider than a
grid step is itself a fidelity limit, not something to bury).

Two independent sources, use whichever has coverage:
  A. Captured Vantage venue bars (data/venue/<broker>_<tf>.jsonl) vs Binance
     footprint (data/footprint/<sym>_<tf>.jsonl), matched by close_ts.
  B. exec_bridge.jsonl command prices (venue-frame) vs the analysis-frame level
     they were rebased from — coarser, fallback only.

Prints median / p10 / p90 / std so the caller can judge stability.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_venue(broker: str, tf: str) -> dict[int, float]:
    p = ROOT / "data" / "venue" / f"{broker}_{tf}.jsonl"
    out: dict[int, float] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        out[int(b["ts"])] = float(b["c"])
    return out


def _load_binance(symbol: str, tf: str) -> dict[int, float]:
    p = ROOT / "data" / "footprint" / f"{symbol}_{tf}.jsonl"
    out: dict[int, float] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        b = json.loads(line)
        out[int(b["close_ts"])] = float(b["ohlc"]["c"])
    return out


def pin(symbol: str = "XAUTUSDT", broker: str = "XAUUSD+", tf: str = "5m",
        match_tol_s: int = 60) -> dict:
    venue = _load_venue(broker, tf)
    binance = _load_binance(symbol, tf)
    if not venue:
        return {"ok": False, "reason": "no captured venue bars yet — run a session with FB_CAPTURE_POLLS / venue logging"}
    if not binance:
        return {"ok": False, "reason": f"no binance footprint for {symbol}/{tf}"}

    b_ts = sorted(binance.keys())
    import bisect
    diffs: list[float] = []
    for vts, vc in venue.items():
        # nearest binance bar within tol
        i = bisect.bisect_left(b_ts, vts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(b_ts) and abs(b_ts[j] - vts) <= match_tol_s:
                if best is None or abs(b_ts[j] - vts) < abs(best - vts):
                    best = b_ts[j]
        if best is not None:
            diffs.append(vc - binance[best])

    if not diffs:
        return {"ok": False, "reason": "no venue/binance ts matched within tol — need more overlap"}

    diffs.sort()
    n = len(diffs)
    res = {
        "ok": True, "n": n,
        "median": round(statistics.median(diffs), 4),
        "mean": round(statistics.fmean(diffs), 4),
        "std": round(statistics.pstdev(diffs), 4) if n > 1 else 0.0,
        "p10": round(diffs[int(0.1 * (n - 1))], 4),
        "p90": round(diffs[int(0.9 * (n - 1))], 4),
        "min": round(diffs[0], 4), "max": round(diffs[-1], 4),
    }
    res["offset"] = res["median"]   # use median as the pin
    return res


def pin_from_cycles(day: str, symbol: str = "XAUTUSDT",
                    broker_symbol: str = "XAUUSD+") -> dict:
    """DO NOT USE AS A VENUE OFFSET — kept only as a diagnostic.

    This differences a cycle's `fulcrum` against the analysis close at `armed_ts`.
    That is NOT the venue basis: a fulcrum is a ZONE EDGE, which legitimately sits
    5-15 points away from spot, so the result is dominated by edge-to-price distance.
    It reported -5.21 for 2026-07-20; the true rebase is ~0.

    The correct measurement is node_rebase_shift() below: live `node_low`/`node_high`
    land on the analysis 0.4 bin grid to within +/-0.12, which proves the zones the
    live server armed on are ANALYSIS-frame, not venue-shifted. Empirically the
    harness scores better at offset 0 than at -5.21 (G1 14.3% vs 12.0%, arm-window
    coverage 118/133 vs 93/133).
    """
    import bisect
    from pipeline.state_store import store

    p = ROOT / "data" / "cycles" / f"cycle_outcomes_{day}.jsonl"
    if not p.exists():
        return {"ok": False, "reason": f"no cycle log for {day}"}
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r.get("broker_symbol") == broker_symbol and r.get("fulcrum")]
    if not rows:
        return {"ok": False, "reason": "no rows with a fulcrum"}

    bars = store().recent(symbol, "1m", 1_000_000)
    if not bars:
        return {"ok": False, "reason": f"no {symbol}/1m bars (check FB_DATA_DIR)"}
    ts = [b.close_ts for b in bars]

    diffs = []
    for r in rows:
        i = bisect.bisect_right(ts, int(r["armed_ts"])) - 1
        if i >= 0:
            diffs.append(float(r["fulcrum"]) - bars[i].ohlc.c)
    if not diffs:
        return {"ok": False, "reason": "no bar matched any arm instant"}

    diffs.sort()
    n = len(diffs)
    return {
        "ok": True, "method": "cycles", "n": n,
        "offset": round(statistics.median(diffs), 4),
        "median": round(statistics.median(diffs), 4),
        "mean": round(statistics.fmean(diffs), 4),
        "std": round(statistics.pstdev(diffs), 4) if n > 1 else 0.0,
        "p10": round(diffs[int(0.1 * (n - 1))], 4),
        "p90": round(diffs[int(0.9 * (n - 1))], 4),
        "min": round(diffs[0], 4), "max": round(diffs[-1], 4),
    }


def node_rebase_shift(day: str, symbol: str = "XAUTUSDT", bin_size: float = 0.4) -> dict:
    """How far live node edges sit off the analysis VP bin grid — the real frame test.

    Cached VP zone edges are exact multiples of vp_bin_size in the analysis frame. If
    the live server had rebased its zones onto the venue, every recorded node edge
    would be offset by the basis. Measuring the residual against the grid therefore
    recovers the actual shift applied.
    """
    p = ROOT / "data" / "cycles" / f"cycle_outcomes_{day}.jsonl"
    if not p.exists():
        return {"ok": False, "reason": f"no cycle log for {day}"}
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    shifts = [float(r["node_low"]) - round(float(r["node_low"]) / bin_size) * bin_size
              for r in rows if r.get("node_low")]
    if not shifts:
        return {"ok": False, "reason": "no node_low values"}
    shifts.sort()
    n = len(shifts)
    return {"ok": True, "n": n, "bin_size": bin_size,
            "median": round(statistics.median(shifts), 4),
            "mean": round(statistics.fmean(shifts), 4),
            "std": round(statistics.pstdev(shifts), 4),
            "p10": round(shifts[int(0.1 * (n - 1))], 4),
            "p90": round(shifts[int(0.9 * (n - 1))], 4),
            "verdict": "zones are ANALYSIS-frame (use offset 0)"
                       if abs(statistics.fmean(shifts)) < bin_size / 2
                       else "zones appear venue-shifted"}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUTUSDT")
    ap.add_argument("--broker", default="XAUUSD+")
    ap.add_argument("--tf", default="5m")
    ap.add_argument("--day", default="", help="diagnostic: fulcrum-vs-close (NOT the venue basis)")
    ap.add_argument("--frame-test", default="", help="day to run the node-grid frame test (preferred)")
    args = ap.parse_args()
    if args.frame_test:
        print(json.dumps(node_rebase_shift(args.frame_test, args.symbol), indent=2))
        raise SystemExit(0)
    if args.day:
        r = pin_from_cycles(args.day, args.symbol, args.broker)
        print(json.dumps(r, indent=2))
        if r.get("ok") and r.get("std", 0) > 1.0:
            print(f"\nWARNING: offset std {r['std']} — basis wanders; a fixed offset "
                  f"misregisters taps by up to {max(abs(r['p10']-r['offset']), abs(r['p90']-r['offset'])):.1f}. "
                  f"Report as a fidelity limit.")
        raise SystemExit(0)
    r = pin(args.symbol, args.broker, args.tf)
    print(json.dumps(r, indent=2))
    if r.get("ok") and r.get("std", 0) > 1.0:
        print(f"\nWARNING: offset std {r['std']} > 1.0 price unit — basis wanders; "
              f"a fixed offset will misregister taps. Report this as a fidelity limit.")
