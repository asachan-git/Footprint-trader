#!/usr/bin/env python3
"""Arm smoke test: drive the LIVE arm path (server/routes/exec_bridge.py) with recorded bars.

Feeds data/footprint/XAUTUSDT_{5m,15m}.jsonl bar by bar into an in-memory state store,
plays each 5m bar's path as EA polls (quote + venue bars), and calls the same arm
functions /exec/poll calls: hvn_inside_touch, lvn_edge_touch, candle_sweep, hvn_edge.
Nothing fills: an armed cycle is retired after --hold bars so the next setup can arm.

    python backtest/arm_smoke.py --from 2026-09-01 --to 2026-09-08

Answers "will it arm on an account with the current config?" — per setup: arms, skip
reasons, legs per side, lot ladder, step. Uses config/settings.yaml as is.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import tempfile
import time
from pathlib import Path

import functools

import pandas as pd
import yaml

# touch_arm_trigger / lvn_touch_arm_trigger re-parse settings.yaml on every call (~90 ms);
# memoize by text so the smoke test runs in minutes. Same parsed values.
_safe_load = yaml.safe_load
_memo = functools.lru_cache(maxsize=8)(lambda text: _safe_load(text))
yaml.safe_load = lambda stream: (__import__("copy").deepcopy(_memo(stream))
                                 if isinstance(stream, str) else _safe_load(stream))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pipeline.state_store as ss  # noqa: E402
import pipeline.features.vp_cache as vpc  # noqa: E402
from freezegun import freeze_time  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="arm_smoke_"))
ss.PERSIST_DIR = _TMP                      # never touch data/footprint
ss._singleton = None
vpc.CACHE_FILE = _TMP / "vp_cache.json"    # never touch data/vp_cache.json

import execution.exec_bridge as eb  # noqa: E402
import server.routes.exec_bridge as route  # noqa: E402
from backtest.run_grid_replay import load  # noqa: E402
from execution.exec_bridge import ExecBridge, PLACE_PENDING, magic_for  # noqa: E402
from pipeline.types import Bar, Level, OHLC  # noqa: E402

ACC, SYM, ANALYSIS = "smoke", "XAUUSD+", "XAUTUSDT"
KINDS = ["hvn_inside_touch", "lvn_edge_touch", "candle_sweep", "hvn_edge"]


def bar(sym, tf, r):
    return Bar(bar_id=f"{sym}|{tf}|{int(r.ts)}", symbol=sym, tf=tf, close_ts=int(r.ts), source="replay",
               ohlc=OHLC(o=float(r.o), h=float(r.h), l=float(r.l), c=float(r.c)),
               bid_ladder=tuple(Level(price=p, vol=v) for p, v in r.bid),
               ask_ladder=tuple(Level(price=p, vol=v) for p, v in r.ask))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t0", required=True)
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--hold", type=int, default=24, help="5m bars before an unfilled arm is retired")
    ap.add_argument("--settings", default=str(ROOT / "config" / "settings.yaml"))
    a = ap.parse_args(argv)
    settings = yaml.safe_load(open(a.settings))
    settings.setdefault("execution", {}).setdefault("symbol_map", {})[ANALYSIS] = SYM

    audits = []
    route._emit_audit = lambda row: audits.append(row)
    for name in ("persist_arm", "persist_emit", "_emit_exit_audit"):
        setattr(eb, name, lambda *x, **k: None)
    eb._emit_cycle_outcome = lambda *x, **k: None
    ExecBridge._audit = classmethod(lambda cls, *x, **k: None)
    ExecBridge.reset()

    b15, b5 = load("15m"), load("5m")
    T = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp())
    t0, t1 = T(a.t0), T(a.t1)
    st = ss.store()
    for r in b15[b15.ts < t0].tail(200).itertuples():
        st.put(bar(ANALYSIS, "15m", r))
    for r in b5[b5.ts < t0].tail(2400).itertuples():
        st.put(bar(ANALYSIS, "5m", r))
    i15 = b15[b15.ts >= t0].itertuples()
    nxt15 = next(i15, None)
    armed_at: dict[int, int] = {}
    plans = collections.defaultdict(list)
    venue = {"5m": [bar(SYM, "5m", r) for r in b5[b5.ts < t0].tail(60).itertuples()],
             "15m": [bar(SYM, "15m", r) for r in b15[b15.ts < t0].tail(60).itertuples()]}
    win = b5[(b5.ts >= t0) & (b5.ts < t1)]
    vp_cfg = settings.get("vp_cache") or {}
    clock = freeze_time(pd.Timestamp(t0, unit="s", tz="UTC").to_pydatetime())
    frozen = clock.start()
    last_vp = 0
    for j, r in enumerate(win.itertuples()):
        frozen.move_to(pd.Timestamp(int(r.ts), unit="s", tz="UTC").to_pydatetime())
        # bars close → store + EA venue bars
        st.put(bar(ANALYSIS, "5m", r)); venue["5m"] = (venue["5m"] + [bar(SYM, "5m", r)])[-60:]
        while nxt15 is not None and nxt15.ts <= r.ts:
            st.put(bar(ANALYSIS, "15m", nxt15)); venue["15m"] = (venue["15m"] + [bar(SYM, "15m", nxt15)])[-60:]
            nxt15 = next(i15, None)
        for tf in ("5m", "15m"):
            ExecBridge.set_venue_bars(ACC, SYM, tf, venue[tf])
        # the server rebuilds the cached daily VP from primary-TF bars every ~15 min
        # (5m bars here: 1m is not in data/footprint), with no venue offset (same prices)
        if r.ts - last_vp >= 900:
            vpc.build_and_save([ANALYSIS], "5m", session_start_utc=vp_cfg.get("session_start_utc", {}),
                               vp_bin_size=vp_cfg.get("vp_bin_size", {}), venue_price_offset={ANALYSIS: 0.0})
            last_vp = r.ts
        # retire stale arms (nothing fills in this smoke test)
        for mg, jj in list(armed_at.items()):
            if j - jj >= a.hold:
                cyc = ExecBridge.get_last_arm(ACC, SYM, magic=mg) or {}
                cyc.pop("magic", None)
                ExecBridge.set_last_arm(ACC, SYM, magic=mg, **{**cyc, "active": False})
                ExecBridge.set_open(ACC, SYM, 0, 0, magic=mg)
                ExecBridge.clear_emit(ACC, SYM, magic=mg)
                del armed_at[mg]
        # the next bar's intrabar path, as polls (o → extreme → extreme → c, with midpoints)
        path = [r.o, r.l, r.h, r.c] if r.c >= r.o else [r.o, r.h, r.l, r.c]
        pts = []
        for x, y in zip(path[:-1], path[1:]):
            pts += [x + (y - x) * k / 4 for k in range(4)]
        pts.append(path[-1])
        for px in pts:
            ExecBridge.set_quote(ACC, SYM, px - 0.1, px + 0.1, now=r.ts)
            before = len(ExecBridge._seq)
            for fn in (route._touch_arm_tf, route._lvn_touch_arm_tf, route._sweep_arm_tf, route._hvn_edge_arm_tf):
                try:
                    fn(ACC, SYM, "15m", settings, venue_mid=px)
                except Exception as e:  # report, keep going
                    audits.append({"verdict": "error", "fn": fn.__name__, "error": repr(e)[:160]})
            new = [ExecBridge._cmds[c] for c in ExecBridge._seq[before:]]
            for mg in {c.magic for c in new if c.type == PLACE_PENDING}:
                legs = [c for c in new if c.magic == mg and c.type == PLACE_PENDING]
                plans[mg].append(dict(ts=int(r.ts), buys=sum(c.order_type == "buy_stop" for c in legs),
                                      sells=sum(c.order_type == "sell_stop" for c in legs),
                                      lots=sorted({c.lot for c in legs}),
                                      step=round(abs(legs[1].price - legs[0].price), 3) if len(legs) > 1 else None))
                armed_at[mg] = j
                nb = sum(c.order_type == "buy_stop" for c in legs); ns = len(legs) - nb
                ExecBridge.set_open(ACC, SYM, 0, len(legs), magic=mg, buy_pendings=nb, sell_pendings=ns)
            ExecBridge._cmds.clear(); ExecBridge._seq.clear()
    clock.stop()

    days = (t1 - t0) / 86400
    res = {}
    for k in KINDS:
        mg = magic_for(k, "15m")
        p = plans.get(mg, [])
        enabled = "15m" in route._trigger_tfs(settings.get("grid_levels") or {}, k)
        skips = collections.Counter(str(x.get("skip_reason", "")).split(":")[1] if ":" in str(x.get("skip_reason", "")) else x.get("skip_reason")
                                    for x in audits if x.get("verdict") == "skip" and x.get("tf") == "15m"
                                    and k.split("_")[0] in str(x.get("skip_reason", "")) + str(x.get("trigger_kind", "")))
        res[k] = dict(enabled_15m=enabled, magic=mg, arms=len(p), arms_per_day=round(len(p) / days, 1),
                      legs_per_side=sorted({(x["buys"], x["sells"]) for x in p})[:6],
                      lot_ladder=sorted({l for x in p for l in x["lots"]}),
                      median_step=float(pd.Series([x["step"] for x in p if x["step"]]).median()) if p else None,
                      top_skips=skips.most_common(4))
    errs = collections.Counter(x["error"] for x in audits if x.get("verdict") == "error")
    for k, v in res.items():
        print(k, json.dumps(v, default=str))
    if errs:
        print("errors:", errs.most_common(5))
    return res


if __name__ == "__main__":
    main()
