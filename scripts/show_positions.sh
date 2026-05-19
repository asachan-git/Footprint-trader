#!/usr/bin/env bash
# Show open + recent closed positions in readable IST format.
cd "$(dirname "$0")/.."
source venv/bin/activate 2>/dev/null
PYTHONPATH=. python3 -c "
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
def ist(ts): return datetime.fromtimestamp(ts, tz=IST).strftime('%Y-%m-%d %H:%M IST')

pfile = Path('data/positions.jsonl')
if not pfile.exists(): print('No positions yet.'); exit()

events = {}
for line in pfile.read_text().splitlines():
    if not line.strip(): continue
    e = json.loads(line)
    events.setdefault(e['position_id'], []).append(e)

open_pos = []
closed_pos = []
for pid, evs in events.items():
    o = next((e for e in evs if e['type']=='open'), None)
    c = next((e for e in evs if e['type'] in ('close','invalidate')), None)
    adj = [e for e in evs if e['type']=='sl_adjust']
    if o:
        (open_pos if not c else closed_pos).append((o, c, adj))

print(f'Open positions: {len(open_pos)}')
print(f'Closed/Invalidated: {len(closed_pos)}')
print()

if open_pos:
    print('═'*70)
    print('OPEN POSITIONS')
    print('═'*70)
    for o, _, adj in open_pos:
        sl = adj[-1]['new_sl'] if adj else o.get('stop_loss')
        print(f\"  {o['side'].upper():6} {o.get('symbol','?'):12} @ {o.get('entry'):.2f}  SL={sl:.2f}  TP={o.get('take_profit'):.2f}\")
        print(f\"    opened: {ist(o['ts'])}\")
        print(f\"    → {o.get('rationale','')[:80]}\")
        if adj: print(f\"    SL adjusted {len(adj)}x — latest: {sl:.2f}\")
        print()

print('═'*70)
print('RECENT CLOSED (last 10)')
print('═'*70)
for o, c, adj in sorted(closed_pos, key=lambda x: x[0]['ts'])[-10:]:
    r = float(c.get('realized_r', 0)) if c else 0
    result = f\"{c['type'].upper()} {r:+.2f}R\" if c else 'OPEN'
    icon = '✅' if r > 0 else '❌'
    print(f\"  {icon} {o['side'].upper():6} {o.get('symbol','?'):12} @ {o.get('entry'):.2f}  {result}\")
    print(f\"    {ist(o['ts'])} → {ist(c['ts']) if c else '-'}\")
    if c: print(f\"    reason: {c.get('reason','')[:70]}\")
    print()
"
