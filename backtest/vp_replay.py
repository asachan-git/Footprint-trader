"""Point-in-time VP prebuild for the grid backtest — the highest-fidelity-risk piece.

The zones ARE the strategy, and live `vp_cache.get()` returns the profile formed
from *bars so far today*, refreshed intraday. A whole-day profile would be lookahead
of exactly the shape that produced the past synthetic-delta artifact. So we prebuild,
per session-day, a SERIES of forming snapshots at a fixed as-of cadence; the seams.py
`get` override then returns the newest snapshot <= the simulated clock.

Everything here calls the REAL vp_cache internals (`_compute_period_vp`, `_day_bounds`,
`_session_day_key`, `_week_*`) so a replayed profile is byte-for-byte what the live
builder would have produced from the same bars. No profile maths is reimplemented.

Output: a cache file in the SAME schema vp_cache uses, but each `daily[date_key]` is a
dict of {as_of_bucket_ts: vp_dict} instead of a single vp_dict. seams.py knows to pick
within it. Weekly is prebuilt whole (a completed prior week has no forming issue; the
current week is coarse enough that intra-week lookahead is negligible for HVN edges —
noted as a known minor limitation).
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.features import vp_cache as vpc
from pipeline.features.volume_profile import DEFAULT_BIN_SIZE

# As-of cadence: one snapshot per this many seconds of session time. 900s (15m)
# matches the coarsest arm TF and bounds the prebuild cost (~96/day/symbol).
BUCKET_S = 900


def _configured_bin_size(symbol: str) -> float | None:
    """vp_bin_size[symbol] from settings — the same source build_and_save uses."""
    try:
        import yaml
        from pathlib import Path as _P
        root = _P(__file__).resolve().parent.parent
        cfg = yaml.safe_load((root / "config" / "settings.yaml").read_text()) or {}
        raw = ((cfg.get("vp_cache") or {}).get("vp_bin_size") or {}).get(symbol)
        return float(raw) if raw is not None else None
    except Exception:
        return None


def build_replay_cache(
    symbol: str,
    out_path: Path,
    *,
    primary_tf: str = "1m",
    anchor_raw=0,
    bin_size: float | None = None,
    bucket_s: int = BUCKET_S,
    venue_offset: float = 0.0,
    only_days: set[str] | None = None,
) -> dict:
    """Prebuild forming-snapshot VP for one symbol over all stored `primary_tf` bars.

    Reads bars from the CURRENT store() (point FB_DATA_DIR at a scratch with the
    live footprint symlinked in, exactly as the harness does). Writes a cache dict
    to out_path and returns it.
    """
    from pipeline.state_store import store as _store

    anchor = vpc._normalize_anchor(anchor_raw)
    if bin_size is None:
        # settings.vp_cache.vp_bin_size FIRST — DEFAULT_BIN_SIZE is only a fallback and
        # differs for gold (1.0 vs the configured 0.4). Using the default quantises every
        # zone edge to whole dollars, so edges never land within the touch buffer of real
        # price and hvn_inside_touch effectively never arms. build_and_save reads the
        # config the same way; the replay must not diverge from it.
        bin_size = _configured_bin_size(symbol) or DEFAULT_BIN_SIZE.get(symbol)

    s = _store()
    all_bars = s.recent(symbol, primary_tf, 1_000_000)
    if not all_bars:
        raise RuntimeError(f"no {symbol}/{primary_tf} bars in store — check FB_DATA_DIR footprint")

    # session-day keys present, in order
    day_keys: list[str] = []
    _seen: set[str] = set()
    for b in all_bars:
        k = vpc._session_day_key(b.close_ts, anchor)
        if k not in _seen:
            _seen.add(k)
            day_keys.append(k)

    # bars are ascending by close_ts (store keeps them sorted); bisect per day so
    # _compute_period_vp filters over ~1 day of bars, not the whole 100k history.
    import bisect as _bisect
    _ts = [b.close_ts for b in all_bars]

    # If a day window is requested, also build the preceding trading day so
    # get_prev_and_today has a prev-D profile (prev-D skips weekends internally).
    want_days: set[str] | None = None
    if only_days:
        want_days = set(only_days)
        from datetime import datetime as _dt, timedelta as _td
        for d in list(only_days):
            base = _dt.strptime(d, "%Y-%m-%d")
            for back in range(1, 5):   # cover a weekend gap
                want_days.add((base - _td(days=back)).strftime("%Y-%m-%d"))

    daily: dict[str, dict] = {}
    for dk in day_keys:
        if want_days is not None and dk not in want_days:
            continue
        start_ts, end_ts = vpc._day_bounds(dk, anchor)
        lo = _bisect.bisect_left(_ts, start_ts)
        hi = _bisect.bisect_left(_ts, end_ts)
        day_bars = all_bars[lo:hi]     # real fn still re-filters [start,as_of) within this
        if not day_bars:
            continue
        snaps: dict[str, dict] = {}
        # forming snapshots every bucket_s across the session, each built from
        # ONLY the bars closed strictly before the as-of instant.
        as_of = start_ts + bucket_s
        while as_of <= end_ts:
            vp = vpc._compute_period_vp(day_bars, start_ts, as_of, bin_size=bin_size)
            if vp is not None:
                snaps[str(as_of)] = vp
            as_of += bucket_s
        # always include the full-day close snapshot at end_ts (last as-of)
        vp_full = vpc._compute_period_vp(day_bars, start_ts, end_ts, bin_size=bin_size)
        if vp_full is not None:
            snaps[str(end_ts)] = vp_full
        if snaps:
            daily[dk] = snaps

    # weekly — completed profile per ISO week (no forming split; see module docstring)
    weekly: dict[str, dict] = {}
    week_keys: list[str] = []
    _wseen: set[str] = set()
    for b in all_bars:
        wk = vpc._week_key(b.close_ts)
        if wk not in _wseen:
            _wseen.add(wk)
            week_keys.append(wk)
    for wk in week_keys:
        ws, we = vpc._week_bounds(wk, anchor)
        wlo = _bisect.bisect_left(_ts, ws)
        whi = _bisect.bisect_left(_ts, we)
        vp = vpc._compute_period_vp(all_bars[wlo:whi], ws, we, bin_size=bin_size)
        if vp is not None:
            weekly[wk] = vp

    cache = {
        symbol: {
            "daily": daily,
            "weekly": weekly,
            "session_start_utc": vpc._anchor_to_storage(anchor),
            "venue_price_offset": float(venue_offset),
            "bucket_s": bucket_s,
            "_forming_snapshots": True,   # marks daily[dk] as {as_of: vp}, not a bare vp
        }
    }
    if bin_size is not None:
        cache[symbol]["bin_size"] = bin_size

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cache))
    return cache


if __name__ == "__main__":
    import argparse
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUTUSDT")
    ap.add_argument("--out", required=True)
    ap.add_argument("--anchor", default="0", help="int UTC hour or 'tz,hour'")
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--bucket", type=int, default=BUCKET_S)
    ap.add_argument("--days", default="", help="comma-sep session-day keys to build (default all)")
    args = ap.parse_args()

    if not os.environ.get("FB_DATA_DIR"):
        raise SystemExit("set FB_DATA_DIR to a scratch dir (with footprint/ symlinked)")

    anchor_raw: object = 0
    if "," in args.anchor:
        tz, hr = args.anchor.split(",")
        anchor_raw = {"tz": tz, "hour": int(hr)}
    else:
        anchor_raw = int(args.anchor)

    only = {d.strip() for d in args.days.split(",") if d.strip()} or None
    c = build_replay_cache(args.symbol, Path(args.out),
                           anchor_raw=anchor_raw, bucket_s=args.bucket,
                           venue_offset=args.offset, only_days=only)
    sym = c[args.symbol]
    ndays = len(sym["daily"])
    nsnaps = sum(len(v) for v in sym["daily"].values())
    print(f"{args.symbol}: {ndays} session-days, {nsnaps} forming snapshots, "
          f"{len(sym['weekly'])} weeks → {args.out}")
