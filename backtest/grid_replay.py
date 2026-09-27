"""Standalone replay of the FootprintBiot hvn_inside_touch 15m grid on XAUTUSDT footprint bars.

Signals: 15m bars, rolling 96-bar volume profile computed with the repo's own
pipeline.features.volume_profile.compute (bin 0.4), hvn_inside_touch rules from
execution/zone_triggers.py, sizing/legs from execution/grid_planner.py.
Execution: 5m bars inside each cycle, O->L->H->C / O->H->L->C path heuristic.
Exit rules mirror ExecBridge.monitor_cycle on origin/exp/pre-server-tp-8d9f546,
plus optional rules from the August review.

Money: USD per standard lot (100 oz): pnl = dPrice * lots * 100.
"""
from __future__ import annotations
import sys, math
from dataclasses import dataclass, field
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
from pipeline.types import Bar, OHLC, Level  # noqa
from pipeline.features.volume_profile import compute as vp_compute  # noqa

NY = ZoneInfo('America/New_York')
FOMC = {pd.Timestamp(d).date() for d in ['2026-06-17', '2026-07-29', '2026-09-16']}

BASE = dict(
    base_lot=0.01, lot_step=0.01, max_lots=0.04, max_legs=5, step_mult=0.5,
    touch_buf=0.2, touch_buf_pct=0.02, tp_atr_mult=1.5,
    target=66.0, decay=0.33, min_target=0.33,
    trail_act=6.67, giveback=0.35, book_frac=0.5,
    leg_tp=True, full_hedge=True, skew=True,
    expiry_bars=8,                     # 15m bars with no fill -> cancel cycle
    spread=0.25, comm_per_lot=6.0,     # round-trip spread $ and commission per std lot
    # --- review rules (off in baseline) ---
    news=False, trapped_k=None, contested_decay=None, opp_cap=None,
    disaster_pts=None, bb_tilt=False, cancel_opp_on_full=False,
)


def to_bar(r, tf):
    return Bar(bar_id=f'X|{tf}|{r.ts}', symbol='XAUTUSDT', tf=tf, close_ts=int(r.ts), source='replay',
               ohlc=OHLC(o=r.o, h=r.h, l=r.l, c=r.c),
               bid_ladder=tuple(Level(price=p, vol=v) for p, v in r.bid),
               ask_ladder=tuple(Level(price=p, vol=v) for p, v in r.ask), poc=None, delta=None)


def precompute(b15: pd.DataFrame, win=96):
    """Rolling HVN zones, ATR14 and BB width slope per 15m bar (as of that bar's close)."""
    bars = [to_bar(r, '15m') for r in b15.itertuples()]
    zones, atr, bbs = [], [], []
    tr = np.maximum(b15.h - b15.l, np.maximum((b15.h - b15.c.shift()).abs(), (b15.l - b15.c.shift()).abs()))
    atr_s = tr.rolling(14).mean().values
    sd = b15.c.rolling(20).std(); w = 4 * sd
    slope = (w / w.shift(3) - 1).values
    for i in range(len(bars)):
        if i < win:
            zones.append([]); continue
        try:
            vp = vp_compute(bars[i - win + 1:i + 1], 'daily', bars[i].ohlc.c, bin_size=0.4)
            zones.append([(float(z['low']), float(z['high'])) for z in (vp.hvn_zones or [])])
        except Exception:
            zones.append([])
    return zones, atr_s, slope


def news_block(ts):
    t = pd.Timestamp(ts, unit='s', tz='UTC').tz_convert(NY)
    if t.weekday() >= 5:
        return False
    m = t.hour * 60 + t.minute
    if 8 * 60 + 25 <= m < 8 * 60 + 50 or 9 * 60 + 55 <= m < 10 * 60 + 20:
        return True
    if t.date() in FOMC and 13 * 60 + 55 <= m < 14 * 60 + 45:
        return True
    return False


def trigger(bar, zones, cfg):
    c, h, l = bar.c, bar.h, bar.l
    best = None
    for lo, hi in zones:
        wdt = hi - lo
        if wdt <= 0 or not (lo < c < hi):
            continue
        be = max(cfg['touch_buf'], wdt * cfg['touch_buf_pct'])
        tt, tb = h >= hi - be, l <= lo + be
        if not (tt or tb):
            continue
        if tt and tb:
            edge, side = (hi, 'top') if abs(hi - c) <= abs(lo - c) else (lo, 'bottom')
        else:
            edge, side = (hi, 'top') if tt else (lo, 'bottom')
        d = abs(edge - c)
        if best is None or d < best[0]:
            best = (d, edge, wdt, side, lo, hi)
    return best


def tps(edge, node, zones, top_leg, bot_leg, atr, mult):
    nlo, nhi = node
    up = sorted([(lo, hi) for lo, hi in zones if hi > top_leg and lo >= nhi - 1e-6], key=lambda z: z[1])
    dn = sorted([(lo, hi) for lo, hi in zones if lo < bot_leg and hi <= nlo + 1e-6], key=lambda z: -z[0])
    tp_up = up[0][1] if up else top_leg + mult * atr
    tp_dn = dn[0][0] if dn else bot_leg - mult * atr
    return tp_up, tp_dn


@dataclass
class Leg:
    side: str; price: float; lot: float
    filled: bool = False; closed: bool = False
    entry: float = 0.0; exit: float = 0.0; sl: float | None = None; tp: float | None = None
    cancelled: bool = False


@dataclass
class Cycle:
    arm_ts: int; fulcrum: float; n: int; step: float; skew: str; legs: list
    target: float; act: float; lotmult: float = 1.0
    peak: float = 0.0; booked: bool = False; max_seen: int = 0
    realized: float = 0.0; exit_reason: str = ''; exit_ts: int = 0; mae: float = 0.0
    capped: bool = False; bars_nofill: int = 0


def pos_pnl(leg, px, cfg):
    d = 1 if leg.side == 'buy' else -1
    return (px - leg.entry) * d * leg.lot * 100


def cost(leg, cfg):
    return leg.lot * cfg['comm_per_lot'] + leg.lot * 100 * cfg['spread']


def run(b15, b5, pre, cfg, t0=None, t1=None):
    zones_all, atr_s, slope = pre
    cfg = {**BASE, **cfg}
    bpt = cfg['base_lot'] * 100               # $ per point for one base lot
    b5ts = b5.ts.values
    cycles, equity = [], []
    i = 96
    N = len(b15)
    realized_total = 0.0
    while i < N:
        r = b15.iloc[i]
        if (t0 and r.ts < t0) or (t1 and r.ts >= t1):
            i += 1; continue
        z = zones_all[i]; atr = atr_s[i]
        if not z or not (atr > 0) or (cfg['news'] and news_block(r.ts)):
            i += 1; continue
        tg = trigger(r, z, cfg)
        if tg is None:
            i += 1; continue
        _, edge, wdt, side, nlo, nhi = tg
        step = cfg['step_mult'] * atr
        n = max(2, min(cfg['max_legs'], int(round(wdt / step))))
        skew = ('sell' if side == 'top' else 'buy') if cfg['skew'] else 'none'
        lm = 1.0
        if cfg['bb_tilt'] and not np.isnan(slope[i]):
            lm = 1.25 if 0 < slope[i] <= 0.25 else (0.75 if slope[i] < -0.10 else 1.0)
        legs = []
        for s, cnt in (('buy', n + (skew == 'buy')), ('sell', n + (skew == 'sell'))):
            for k in range(1, cnt + 1):
                lot = min(cfg['max_lots'], cfg['base_lot'] + (k - 1) * cfg['lot_step']) * lm
                px = edge + k * step if s == 'buy' else edge - k * step
                legs.append(Leg(s, px, lot))
        top = max(l.price for l in legs if l.side == 'buy'); bot = min(l.price for l in legs if l.side == 'sell')
        tpu, tpd = tps(edge, (nlo, nhi), z, top, bot, atr, cfg['tp_atr_mult'])
        if cfg['leg_tp']:
            for l in legs:
                l.tp = tpu if l.side == 'buy' else tpd
        cyc = Cycle(int(r.ts), edge, n, step, skew, legs, cfg['target'], cfg['trail_act'], lm)
        # ---- execute on 5m bars after the 15m close ----
        j = int(np.searchsorted(b5ts, r.ts, side='right'))
        done = False
        prev_c = r.c
        while j < len(b5) and not done:
            b = b5.iloc[j]
            if cfg['news'] and news_block(b.ts - 300) and any(l.filled and not l.closed for l in legs):
                done = close_all(cyc, b.o, cfg, 'news', b.ts); break
            po = cfg.get('path', 'auto')
            if po == 'auto':
                path = [b.o, b.l, b.h, b.c] if b.c >= b.o else [b.o, b.h, b.l, b.c]
            elif po == 'hl':
                path = [b.o, b.h, b.l, b.c]
            else:
                path = [b.o, b.l, b.h, b.c]
            if prev_c is not None and prev_c != b.o:
                path = [prev_c] + path          # gap between bars (weekend / missing data)
            prev_c = b.c
            for a, c2 in zip(path[:-1], path[1:]):
                done = walk(cyc, a, c2, cfg, bpt, b.ts)
                if done: break
            if not done:
                anyfill = any(l.filled for l in legs)
                if not anyfill:
                    cyc.bars_nofill += 1
                    if cyc.bars_nofill >= cfg['expiry_bars'] * 3:
                        for l in legs: l.cancelled = True
                        cyc.exit_reason = 'expired'; cyc.exit_ts = int(b.ts); done = True
            fl = sum(pos_pnl(l, b.c, cfg) for l in legs if l.filled and not l.closed)
            equity.append((int(b.ts), realized_total + cyc.realized + fl))
            j += 1
        if not done and j >= len(b5):
            close_all(cyc, b5.c.iloc[-1], cfg, 'end', int(b5.ts.iloc[-1]))
        realized_total += cyc.realized
        cycles.append(cyc)
        # next arm only after this cycle ends (one cycle per strat x TF)
        i = int(np.searchsorted(b15.ts.values, cyc.exit_ts, side='right'))
    return cycles, equity


def open_legs(cyc):
    return [l for l in cyc.legs if l.filled and not l.closed]


def net_pnl(cyc, px, cfg):
    return cyc.realized + sum(pos_pnl(l, px, cfg) for l in open_legs(cyc))


def close_leg(cyc, l, px, cfg):
    l.closed = True; l.exit = px
    cyc.realized += pos_pnl(l, px, cfg) - cost(l, cfg)


def close_all(cyc, px, cfg, reason, ts):
    for l in open_legs(cyc):
        close_leg(cyc, l, px, cfg)
    for l in cyc.legs:
        if not l.filled: l.cancelled = True
    cyc.exit_reason = reason; cyc.exit_ts = int(ts)
    return True


def walk(cyc, a, b, cfg, bpt, ts):
    """Move price a->b; process stop fills, leg TPs and BE stops in order; run monitor."""
    up = b >= a
    ev = []
    for l in cyc.legs:
        if l.cancelled: continue
        if not l.filled:
            if l.side == 'buy' and up and a < l.price <= b: ev.append((l.price, 'fill', l))
            if l.side == 'sell' and not up and b <= l.price < a: ev.append((l.price, 'fill', l))
        elif not l.closed:
            if l.tp is not None:
                if l.side == 'buy' and up and a < l.tp <= b: ev.append((l.tp, 'tp', l))
                if l.side == 'sell' and not up and b <= l.tp < a: ev.append((l.tp, 'tp', l))
            if l.sl is not None:
                if l.side == 'buy' and not up and b <= l.sl < a: ev.append((l.sl, 'sl', l))
                if l.side == 'sell' and up and a < l.sl <= b: ev.append((l.sl, 'sl', l))
    ev.sort(key=lambda e: e[0], reverse=not up)
    for px, kind, l in ev:
        if l.cancelled or l.closed: continue
        if kind == 'fill':
            if l.filled: continue
            l.filled = True; l.entry = px
            if cfg['opp_cap'] is not None:
                apply_opp_cap(cyc, cfg)
            if cfg['cancel_opp_on_full']:
                sd = [x for x in cyc.legs if x.side == l.side and not x.cancelled]
                if all(x.filled for x in sd):
                    for x in cyc.legs:
                        if x.side != l.side and not x.filled: x.cancelled = True
                        if x.side != l.side and x.filled and not x.closed and cfg.get('close_opp_filled', True):
                            close_leg(cyc, x, px, cfg)   # don't leave an orphan hedge leg
                        if x.side == l.side and x.filled and not x.closed and x.sl is None:
                            x.sl = x.entry      # fullfill_be: committed side to break-even
        elif kind in ('tp', 'sl'):
            close_leg(cyc, l, px, cfg)
            # flatten-rest: a filled leg closed (TP or BE stop) while the ladder still rests
            if any(not x.filled and not x.cancelled for x in cyc.legs):
                return close_all(cyc, px, cfg, 'leg_tp' if kind == 'tp' else 'leg_closed_other', ts)
        if monitor(cyc, px, cfg, bpt, ts): return True
    return monitor(cyc, b, cfg, bpt, ts)


def apply_opp_cap(cyc, cfg):
    for s in ('buy', 'sell'):
        o = 'sell' if s == 'buy' else 'buy'
        fs = sum(1 for x in cyc.legs if x.side == s and x.filled)
        fo = sum(1 for x in cyc.legs if x.side == o and x.filled)
        if fs >= 2 and fo == 0:
            pend = sorted([x for x in cyc.legs if x.side == o and not x.filled and not x.cancelled],
                          key=lambda x: abs(x.price - cyc.fulcrum))
            for k, x in enumerate(pend):
                if k >= cfg['opp_cap']: x.cancelled = True
                else: x.lot = cfg['base_lot'] * cyc.lotmult


def monitor(cyc, px, cfg, bpt, ts):
    ol = open_legs(cyc)
    buys = sum(1 for l in ol if l.side == 'buy'); sells = sum(1 for l in ol if l.side == 'sell')
    pend = sum(1 for l in cyc.legs if not l.filled and not l.cancelled)
    if not ol:
        if any(l.filled for l in cyc.legs) and pend == 0:
            cyc.exit_reason = cyc.exit_reason or 'all_closed'; cyc.exit_ts = int(ts); return True
        if cyc.booked and not ol:
            return close_all(cyc, px, cfg, 'booked_out', ts)
        return False
    net = net_pnl(cyc, px, cfg)
    cyc.mae = min(cyc.mae, net)
    # disaster cap
    if cfg['disaster_pts'] is not None and net <= -cfg['disaster_pts'] * bpt * cyc.lotmult:
        return close_all(cyc, px, cfg, 'disaster', ts)
    # trapped
    if cfg['trapped_k'] is not None and min(buys, sells) >= cfg['trapped_k']:
        return close_all(cyc, px, cfg, 'trapped', ts)
    # bias trail (once)
    if not cyc.booked:
        cyc.peak = max(cyc.peak, net)
        if cyc.peak >= cyc.act * cyc.lotmult and net > 0 and net <= cyc.peak * (1 - cfg['giveback']):
            bpnl = sum(pos_pnl(l, px, cfg) for l in ol if l.side == 'buy')
            spnl = sum(pos_pnl(l, px, cfg) for l in ol if l.side == 'sell')
            bias = 'buy' if bpnl >= spnl else 'sell'
            side_legs = sorted([l for l in ol if l.side == bias], key=lambda x: -x.lot)
            k = max(1, int(round(len(side_legs) * cfg['book_frac'])))
            for l in side_legs[:k]:
                close_leg(cyc, l, px, cfg)
            for l in side_legs[k:]:
                l.sl = l.entry
            cyc.booked = True
            return False
    # net target with hedge decay
    hi = max(buys, sells); hr = min(buys, sells) / hi if hi else 0.0
    decay = cfg['decay']
    if cfg['contested_decay'] is not None and min(buys, sells) >= 1:
        decay = cfg['contested_decay']
    eff = max(cfg['min_target'], cyc.target * cyc.lotmult * (1 - decay * hr))
    if net >= eff:
        return close_all(cyc, px, cfg, 'net_target', ts)
    # full hedge
    if cfg['full_hedge'] and min(buys, sells) >= cyc.n and buys + sells >= 2 * cyc.n:
        return close_all(cyc, px, cfg, 'full_hedge', ts)
    return False


def summarize(cycles, equity):
    df = pd.DataFrame([dict(ts=c.arm_ts, exit_ts=c.exit_ts, pnl=c.realized, reason=c.exit_reason, mae=c.mae,
                            filled=sum(l.filled for l in c.legs),
                            buys=sum(l.filled and l.side == 'buy' for l in c.legs),
                            sells=sum(l.filled and l.side == 'sell' for l in c.legs),
                            held=(c.exit_ts - c.arm_ts) / 60) for c in cycles])
    traded = df[df.filled > 0]
    eq = pd.Series([e[1] for e in equity])
    dd = (eq - eq.cummax()).min() if len(eq) else 0
    w = traded.pnl > 0
    gl = -traded.pnl[~w].sum()
    return dict(cycles=len(traded), net=round(traded.pnl.sum(), 1),
                pf=round(traded.pnl[w].sum() / gl, 2) if gl > 0 else float('inf'),
                win=round(100 * w.mean(), 1) if len(traded) else 0,
                avg_win=round(traded.pnl[w].mean(), 2) if w.any() else 0,
                avg_loss=round(traded.pnl[~w].mean(), 2) if (~w).any() else 0,
                worst=round(traded.pnl.min(), 1) if len(traded) else 0,
                max_dd=round(dd, 1),
                med_hold_h=round(traded.held.median() / 60, 1) if len(traded) else 0,
                max_hold_h=round(traded.held.max() / 60, 1) if len(traded) else 0), df
