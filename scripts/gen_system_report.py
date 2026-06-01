import json
from collections import Counter
from datetime import datetime, timezone

# ---------- load cycles ----------
opens={}; closes=[]
for l in open('data/cycles.jsonl'):
    d=json.loads(l)
    if d['type']=='open': opens[d['cycle_id']]=d
    elif d['type']=='close': closes.append(d)
rows=[]
for c in closes:
    o=opens.get(c['cycle_id'],{})
    rows.append(dict(sym=o.get('symbol','?'),dir=o.get('direction','?'),
        pnl=c.get('realized_pnl',0.0),reason=c.get('reason','?'),
        ts=c.get('ts',0),bv=c.get('bar_verified',False)))
rows.sort(key=lambda r:r['ts'])

def st(rs):
    n=len(rs)
    if not n: return None
    w=[r for r in rs if r['pnl']>0]; ls=[r for r in rs if r['pnl']<=0]
    tot=sum(r['pnl'] for r in rs)
    gp=sum(r['pnl'] for r in w); gl=abs(sum(r['pnl'] for r in ls))
    return dict(n=n,wr=len(w)/n*100,tot=tot,avg=tot/n,nw=len(w),nl=len(ls),
        aw=gp/len(w) if w else 0,al=-gl/len(ls) if ls else 0,
        pf=(gp/gl if gl else float('inf')),
        rr=(gp/len(w))/(gl/len(ls)) if (w and ls and gl>0) else float("inf"))

def row_md(label,s):
    if not s: return f"| {label} | 0 | – | – | – | – | – | – |"
    pf = "∞" if s['pf']==float('inf') else f"{s['pf']:.2f}"
    rr = "∞" if s['rr']==float('inf') else f"{s['rr']:.2f}"
    return (f"| {label} | {s['n']} | {s['wr']:.1f}% | {s['tot']:+.2f} | "
            f"{s['avg']:+.4f} | {s['aw']:+.4f} | {s['al']:+.4f} | {rr} |")

ov=st(rows)
syms=sorted(set(r['sym'] for r in rows))

# ---------- equity curve ascii ----------
cum=0; curve=[]
for r in rows: cum+=r['pnl']; curve.append(cum)
pts=[0.0]+curve
W=58;H=15
mn=min(pts);mx=max(pts);rng=(mx-mn) or 1
n=len(pts)
cols=[pts[int(round(i*(n-1)/(W-1)))] for i in range(W)]
grid=[[' ']*W for _ in range(H)]
for x,v in enumerate(cols):
    y=H-1-int(round((v-mn)/rng*(H-1)))
    grid[y][x]='+'
clines=[]
for r in range(H):
    val=mx-(r/(H-1))*rng
    clines.append(f"{val:+6.1f} |"+''.join(grid[r]))
clines.append("       +"+"-"*W)
clines.append("        trade 1"+" "*(W-16)+f"trade {len(curve)}")
chart="\n".join(clines)

# drawdown
peak=-1e9;maxdd=0
for v in curve:
    peak=max(peak,v); maxdd=min(maxdd,v-peak)

# ---------- positions ----------
psrc=Counter();pside=Counter();psym=Counter();ptype=Counter()
for l in open('data/positions.jsonl'):
    d=json.loads(l);ptype[d.get('type')]+=1
    if d.get('type')=='open':
        psrc[d.get('source')]+=1;pside[d.get('side')]+=1;psym[d.get('symbol')]+=1

# ---------- mode_compare M2 ----------
mc=[json.loads(l) for l in open('data/mode_compare.jsonl')]
m2side=Counter(m.get('side') for m in mc)
m2sym=Counter(m.get('symbol') for m in mc if m.get('side') in ('long','short'))
reasons=Counter(r['reason'] for r in rows)

def ist(ts): return datetime.fromtimestamp(ts,timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
first=ist(rows[0]['ts']); last=ist(rows[-1]['ts'])

# legs/cycle
legs_per = ptype['open']/ov['n']

doc=f"""# System Performance Report — FootprintBiot

Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} · Source: live paper log (committed + current on-disk data)
Window: {first} → {last} · Engine: **M1 (Claude)**, paper-simulated fills

> Companion to [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) (how it works) and [PLAN.md](PLAN.md) (roadmap).
> This file is the **numbers**: every figure below is computed from `data/cycles.jsonl`,
> `data/positions.jsonl`, and `data/mode_compare.jsonl`. R = realized risk units vs the disaster floor.

---

## 1. Headline

| Metric | Value |
|---|---|
| Closed cycles (trades) | **{ov['n']}** |
| Win rate | **{ov['wr']:.1f}%** ({ov['nw']}W / {ov['nl']}L) |
| Total R | **{ov['tot']:+.2f}R** |
| Avg R / trade | **{ov['avg']:+.4f}R** |
| Avg win | {ov['aw']:+.4f}R |
| Avg loss | {ov['al']:+.4f}R |
| Payoff (avg win / avg loss) | {ov['rr']:.2f} |
| Profit factor | {ov['pf']:.2f} |
| Max drawdown | {maxdd:+.3f}R |
| Position legs filled | {ptype['open']} (≈{legs_per:.2f} legs/cycle) |

**Read:** high WR, small avg R, fat profit factor — classic grid-recovery signature.
Wins are frequent and small; the few losses are contained (max DD {maxdd:+.3f}R = near-flat equity).

---

## 2. Equity curve (cumulative R, {len(curve)} closed cycles)

```
{chart}
```

Final **{ov['tot']:+.2f}R**, peak {mx:+.2f}R, trough {mn:+.2f}R, max drawdown {maxdd:+.3f}R.
Monotonic-ish climb — drawdowns are shallow because nano-lot legs average down and exit on bounce.

---

## 3. By symbol

| Symbol | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
{chr(10).join(row_md(s,st([r for r in rows if r['sym']==s])) for s in syms)}
{row_md('**ALL**',ov)}

XAU carries per-trade edge (avg {st([r for r in rows if r['sym']=='XAUTUSDT'])['avg']:+.4f}R); BTC is positive but thinner and owns the fattest single losses.

---

## 4. By direction (buy / sell split)

| Side | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
{row_md('Long (buy)',st([r for r in rows if r['dir']=='long']))}
{row_md('Short (sell)',st([r for r in rows if r['dir']=='short']))}

Book skews short ({st([r for r in rows if r['dir']=='short'])['n']} short vs {st([r for r in rows if r['dir']=='long'])['n']} long); both sides profitable, near-equal payoff.

---

## 5. Symbol × direction

| Bucket | Trades | WR | Total R | Avg R | Avg win | Avg loss | Payoff |
|---|---|---|---|---|---|---|---|
{chr(10).join(row_md(f"{s} {d}",st([r for r in rows if r['sym']==s and r['dir']==d])) for s in syms for d in ['long','short'])}

Best bucket: **XAU long** (WR {st([r for r in rows if r['sym']=='XAUTUSDT' and r['dir']=='long'])['wr']:.0f}%, avg {st([r for r in rows if r['sym']=='XAUTUSDT' and r['dir']=='long'])['avg']:+.4f}R).
Weakest: **BTC long** (avg {st([r for r in rows if r['sym']=='BTCUSDT' and r['dir']=='long'])['avg']:+.4f}R, payoff {st([r for r in rows if r['sym']=='BTCUSDT' and r['dir']=='long'])['rr']:.2f}).

---

## 6. Exit reasons

| Reason | Count | Share |
|---|---|---|
{chr(10).join(f"| {k} | {v} | {v/ov['n']*100:.0f}% |" for k,v in reasons.most_common())}

Note: `sl_hit` here are mostly **trailed-stop profit locks** (positive R), not disaster exits —
disaster_floor fired only {reasons.get('disaster_floor',0)}× and choch_invalidation {reasons.get('choch_invalidation',0)}×.

---

## 7. Order flow (position legs, not cycles)

All legs sourced from **{list(psrc)[0]}** (M1 live; M2 still dry-run).

| Cut | Counts |
|---|---|
| Open / close legs | {ptype['open']} / {ptype['close']} |
| Side | short {pside['short']} · long {pside['long']} |
| Symbol | XAU {psym['XAUTUSDT']} · BTC {psym['BTCUSDT']} |

≈{legs_per:.2f} legs per cycle → grid mostly fired single-leg, occasionally averaged in.

---

## 8. M2 (rules engine) — dry-run signal mix

M2 logs to `data/mode_compare.jsonl` but does **not** trade yet (no realized R to report).
Signal distribution over {len(mc)} bars:

| Signal | Count |
|---|---|
| short | {m2side['short']} |
| long | {m2side['long']} |
| flat (no trade) | {m2side['flat']} |

M2 fires on {(m2side['short']+m2side['long'])/len(mc)*100:.0f}% of bars; the rest are filtered flat (|score| < 0.35).

---

## 9. Caveats

- Single regime epoch ({first[:10]} → {last[:10]}); WR/payoff will compress out of sample.
- Fills are **paper-simulated**; no slippage/spread/funding modeled.
- `realized_pnl` taken from `cycles.jsonl` close events (bar-verified flag present on the early batch).
- M2 numbers are signal-only — no execution, so not comparable to M1's realized R here.
- These are **committed-state + current on-disk** figures; re-run `/tmp/gen_report.py` after new data.
"""
open('SYSTEM_REPORT.md','w').write(doc)
print("WROTE SYSTEM_REPORT.md", len(doc),"bytes")
