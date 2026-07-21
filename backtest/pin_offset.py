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


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUTUSDT")
    ap.add_argument("--broker", default="XAUUSD+")
    ap.add_argument("--tf", default="5m")
    args = ap.parse_args()
    r = pin(args.symbol, args.broker, args.tf)
    print(json.dumps(r, indent=2))
    if r.get("ok") and r.get("std", 0) > 1.0:
        print(f"\nWARNING: offset std {r['std']} > 1.0 price unit — basis wanders; "
              f"a fixed offset will misregister taps. Report this as a fidelity limit.")
