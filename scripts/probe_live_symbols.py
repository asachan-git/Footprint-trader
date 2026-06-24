"""Probe the live MT5 account for tradable XAU + BTC symbol names.

Usage:
    PYTHONPATH=. venv/bin/python scripts/probe_live_symbols.py [port]

Requires the wine rpyc server running (scripts/mt5_server.sh).
First lists ALL symbols matching *XAU* and *BTC* via symbols_get,
then does a tick probe on each to confirm it's live + in Market Watch.
"""
import sys
PYTHONPATH = "."

from execution.mt5_direct import MT5Direct

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18812
c = MT5Direct(port=PORT)

print("=== account ===")
try:
    a = c.account_info()
    print(f"  login={a.get('login')}  server={a.get('server')}  "
          f"balance={a.get('balance')}  currency={a.get('currency')}")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

print()
for group, label in [("*XAU*", "GOLD"), ("*BTC*", "BTC"), ("*GLD*", "GLD-alt")]:
    print(f"=== symbols_get('{group}') [{label}] ===")
    try:
        syms = c.symbols_get(group)
    except Exception as e:
        print(f"  symbols_get failed: {e}")
        syms = []
    if not syms:
        print("  (none)")
    for s in syms:
        name = s["name"]
        tick = c.tick(name)
        bid = tick.get("bid", 0)
        ask = tick.get("ask", 0)
        live = "LIVE" if bid > 0 else "EMPTY"
        print(f"  {name:<20} {live:6}  bid={bid}  ask={ask}  "
              f"digits={s['digits']}  min_vol={s['volume_min']}  visible={s.get('visible')}")
    print()

# Also probe common name variants directly
print("=== direct tick probes ===")
CANDIDATES = [
    "XAUUSD", "XAUUSD+", "XAUUSD.pc", "XAUUSD.pro", "XAUUSDm",
    "BTCUSD", "BTCUSD+", "BTCUSD.pc", "BTCUSD.pro", "BTCUSDm",
    "BTCUSDT",
]
for sym in CANDIDATES:
    try:
        info = c.symbol_info(sym)
        tick = c.tick(sym)
        if not info:
            print(f"  {sym:<20} NO syminfo")
            continue
        bid = tick.get("bid", 0)
        live = "LIVE" if bid > 0 else "EMPTY-TICK"
        print(f"  {sym:<20} {live:12}  bid={bid}  digits={info.get('digits')}  "
              f"min={info.get('volume_min')}  visible={info.get('visible')}")
    except Exception as e:
        print(f"  {sym:<20} ERROR: {e}")
