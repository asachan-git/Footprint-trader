# jul09 branch — live grid execution runbook

Footprint/orderflow-driven grid trading on gold (XAUUSD) via MetaTrader 5 (Vantage Markets),
account **24678823** (VantageMarkets-Live 21, USC cent account). This branch runs the
`hvn_inside_touch` / `hvn_edge` / `lvn_edge_touch` grid strategies with server-side exit
management (net-target, side-completion trail, cycle-level trail, hedge cutoff).

**This is a live-money system.** If you just want things to stop safely, jump to
"Stopping" — the fast path is right at the top of that section.

`README.md` in this repo is an older architecture doc from an earlier (non-grid) phase of
the project — it does not describe what's below. This is the operational doc for the system
actually running live.

---

## Architecture — five things have to be running together

```
┌──────────────────┐
│  binance.main     │──── HTTP POST /ingest ─────┐
│  XAUTUSDT 1m       │                             │
│  REST poller       │                             ▼
└──────────────────┘                  ┌─────────────────────┐
┌─────────────────────┐     ┌──────────────────┐     │  jul09 Flask server │
│  MT5 terminal        │◄───►│  mt5_direct       │◄───►│  server.app          │
│  (wine, macOS)        │     │  bridge (wine-py,  │     │  :5002               │
│  terminal64.exe       │     │  RPyC :18812)       │     │  (grid brain, exits) │
│  + FBExecBridge_Jul09 │     └──────────────────┘     │                      │
│    EA on XAUUSD.pc    │◄──── HTTP poll/ack (~1s) ────►│                      │
└─────────────────────┘                                └─────────────────────┘
```

1. **`binance.main` XAUTUSDT ingestion poller** — `python -m binance.main --symbol XAUUSDT
   --symbol-as XAUTUSDT --tf 1m --price-step 0.1 --venue futures --rest --flask
   http://localhost:5002` (Binance Futures REST, since the WS feed is geo-blocked). This is
   the **analysis-frame price/footprint feed** the whole HVN/LVN/VP engine is built on — a
   totally separate symbol/venue from the MT5 execution side (Binance XAUTUSDT vs Vantage
   XAUUSD.pc), reconciled via the venue-offset/rebase logic elsewhere in the codebase.
   **This is not started by any script referenced below** — it was found already running as
   a standalone background process (started 2026-08-06, independent of any Flask restart
   tonight). If it ever dies, footprint bars stop arriving and `[gap-monitor]` will alert;
   restart with the command above from the repo root (any Python 3, not the wine one).
2. **MT5 terminal** — the actual broker connection. Runs under Wine (`MetaTrader 5.app`),
   logged into 24678823 @ VantageMarkets-Live 21, **AutoTrading button must be green**.
3. **`mt5_direct` bridge** (`scripts/mt5_server.sh`, port 18812) — a wine-python process
   running the official `MetaTrader5` pip package (Windows-only, hence wine) alongside the
   terminal, exposing direct position/order read+write over RPyC to the mac-side Python.
   This is how the server gets **broker-truth** data (see "Why the bridge matters" below) —
   it is a *second*, independent path into the account, not just a convenience.
4. **jul09 Flask server** (`server.app`, port 5002, `FB_MAGIC_BASE=776000`) — the actual grid
   brain. Computes HVN/LVN zones, decides when to arm a straddle, tracks per-cycle P&L, fires
   exits (net_target, sidefull_trail, bias_trail, full_hedge). The EA polls this every ~1s.
5. **`FBExecBridge_Jul09.mq5` EA**, attached to the XAUUSD.pc chart in the terminal — places
   orders per the server's instructions and reports back live position/order state. Chart
   Inputs must have `InpMagic = 776000`, `InpMagicRange = 150`, `InpBridgeURL =
   http://127.0.0.1:5002`. **These are chart-saved inputs, not source defaults** — a
   recompile does NOT reset them; only editing the EA's Inputs tab (or detach/reattach) does.

**Why the bridge matters**: the EA is the only thing that talks to the broker for placing
orders, but it self-reports its own position/order state to the server on each poll — and
that self-report can go stale or omit magics (e.g. if `InpMagicRange` on the chart doesn't
cover a strategy's magic). The `mt5_direct` bridge lets the server independently verify —
and override — the EA's self-report with real broker state on every poll
(`server/routes/exec_bridge.py`'s `_merge_broker_magics`). If the bridge dies, the server
falls back to trusting the EA alone, which is the exact condition that caused a real missed
profit-lock — see `execution/peak_audit.py`'s docstring for the incident. **If the bridge is
down for more than 15s, the server logs a loud `*** BROKER-TRUTH FALLBACK DOWN ***` error**
— watch for that line.

---

## Prerequisites (one-time setup)

- macOS with **Wine** (via `MetaTrader 5.app` — MetaQuotes' own wine-wrapped installer)
- MT5 terminal logged into VantageMarkets-Live 21, account 24678823, with a Windows Python
  3.12 installed **inside the wine prefix** (`C:\python312\python.exe`) with `MetaTrader5`,
  `numpy==1.26.4` (not 2.x — hits a wine ucrtbase gap), and `rpyc` pip-installed
- Mac-side: `venv/` in the repo root with this branch's `requirements.txt` installed
- `FBExecBridge_Jul09.mq5` compiled in MetaEditor and attached to an XAUUSD.pc chart, with
  Inputs set as described above (`InpMagic=776000`, `InpMagicRange=150`,
  `InpBridgeURL=http://127.0.0.1:5002`)
- Historical footprint data present (`data/footprint/`, gitignored — not in the repo) — the
  server refuses to start on thin/missing data (`pipeline/startup_check.assert_ready`, gated
  on XAUTUSDT, requires ≥5 days). **See "Cold start" below — this is the one prerequisite
  that does NOT have a clean automated fix for a fresh clone.**

---

## Starting everything, end to end

Order matters — each step depends on the previous one being alive.

```bash
# 1. XAUTUSDT ingestion poller — the analysis-frame data feed everything else is built on.
#    Check first, it may already be running (it survives Flask restarts):
ps aux | grep "binance.main" | grep -v grep
#    If not running:
cd /Users/aniteksachan/Strategies/FootprintBiot-jul09
nohup python3 -m binance.main --symbol XAUUSDT --symbol-as XAUTUSDT --tf 1m \
  --price-step 0.1 --venue futures --rest --flask http://localhost:5002 \
  > /tmp/xaut_ingest.log 2>&1 &
disown

# 2. MT5 terminal — open it (or confirm it's already running), log in, click AutoTrading
#    green if not already. This is a GUI step, no command for it.

# 3. mt5_direct bridge — launches wine-python inside the MT5 wine prefix, connects to
#    the running terminal. Can be run from ANY worktree (the script path is hardcoded
#    to the main repo's execution/mt5_bridge/server.py via wine's Z: drive).
cd /Users/aniteksachan/Strategies/FootprintBiot-jul09
nohup bash scripts/mt5_server.sh 18812 > /tmp/mt5_bridge.log 2>&1 &
disown
# wait for it, then confirm:
lsof -nP -iTCP:18812 -sTCP:LISTEN
tail -5 /tmp/mt5_bridge.log     # should show "MT5 connected: 24678823 ..."

# 4. jul09 Flask server — the grid brain. Takes ~30-60s (VP precompute on startup).
#    This is the step that will hard-block (SystemExit) if footprint data is missing/thin
#    — see "Cold start" below if this is a fresh clone.
cd /Users/aniteksachan/Strategies/FootprintBiot-jul09
FLASK_PORT=5002 FB_MAGIC_BASE=776000 venv/bin/python -m server.app > /tmp/jul09_server.log 2>&1 &
disown
# wait for it (poll until listening), then confirm:
lsof -nP -iTCP:5002 -sTCP:LISTEN
grep "arm state restored\|gap-monitor\|peak-audit" /tmp/jul09_server.log

# 5. EA — should already be attached and polling if the chart was left open. If not,
#    drag FBExecBridge_Jul09.mq5 onto the XAUUSD.pc chart, verify Inputs (step above),
#    and confirm AutoTrading is green. Watch /tmp/jul09_server.log for live
#    "[touch_arm]"/"[touch_arm_lvn]" lines — that means the EA is polling successfully.
```

**Do NOT use `scripts/start.sh` / `scripts/restart_server.sh` for this branch** — those are
leftover from an earlier "Mode 1 / Mode 2" paper-trading architecture (Binance ingest +
`/decide_multi` + `/grid_tick` paper A/B), hardcoded to port 5000, and unrelated to the live
grid-exec system described here. Running them alongside the real stack would start a
conflicting, irrelevant second server. `scripts/run_server.sh` is closer (it does start
`server.app`) but doesn't set `FB_MAGIC_BASE`/`FLASK_PORT` — use the manual command above, or
export the env vars first if you adapt it.

### Verifying it's actually healthy (not just running)

```bash
# broker bridge responding
venv/bin/python -c "
from execution.mt5_direct import get_client
c=get_client(); a=c.account_info()
print('balance', a['balance'], 'equity', a['equity'])"

# 0 tracebacks, log activity within the last few seconds
grep -c Traceback /tmp/jul09_server.log
tail -2 /tmp/jul09_server.log

# no stale-bridge or peak-divergence alerts
grep -c "BROKER-TRUTH FALLBACK DOWN\|PEAK DIVERGENCE" /tmp/jul09_server.log
```

---

## Cold start — cloning fresh on a new machine

**Short answer: no, there is not currently a clean automated mechanism for this**, and it's
worth knowing before you try.

`data/footprint/` (per-symbol/TF bar history, what the whole HVN/LVN/VP engine is built on)
and `data/vp_cache.json` are both gitignored — a fresh clone has neither. `server.app`'s
startup preflight (`pipeline/startup_check.assert_ready`, `startup_check.enabled: true` by
default) hard-blocks (`SystemExit`) unless XAUTUSDT has **≥5 days** of footprint history with
a fresh volume profile carrying HVN zones. On this machine, that requirement is currently
satisfied by an accumulated `data/footprint/` directory built up over this branch's whole
lifetime by the `binance.main` ingestion poller (component 1 above) — it isn't something any
script rebuilds on demand.

What exists for backfilling history:
- `scripts/rebuild_footprint_history.py` — downloads real footprint from **Bybit's public
  bulk trade files**, but is **BTCUSDT-only** (its own docstring: *"XAUTUSDT spot is not
  available in Bybit's public bulk data"*). It does not help XAUTUSDT, which is the symbol
  this branch actually trades.
- Nothing else in this repo backfills XAUTUSDT history. The only path that produces real
  XAUTUSDT bars is running the live `binance.main` poller forward in real time.

**Practical options for a fresh clone, in order of speed:**
1. **Copy the data directory from a machine that already has it** — `data/footprint/` and
   `data/vp_cache.json` from this machine (or another already-running instance) satisfy the
   preflight immediately. Fastest, and what you'd want for anything beyond a quick
   read-only/dev check.
2. **Let it run forward for real** — start the `binance.main` poller and wait `min_days` (5
   real days) before the preflight will pass on its own. Only realistic for a genuinely new
   long-term deployment, not for getting something running today.
3. **Bypass the gate for non-live use** — set `startup_check.enabled: false` in
   `config/settings.yaml`. The code's own comment on this flag is explicit: *"Disable via
   startup_check.enabled=false (**not for live**)"* — this is fine for reading code, running
   tests, or a dry inspection of the server, but the server will then happily try to trade
   on empty/thin VP data if you also connect it to a real broker. Don't use this for a live
   account.

If this needs to become a real "clone and go" setup (e.g. for a second machine or a CI
environment), the actual work would be writing a real XAUTUSDT historical backfill (Binance
Futures has historical kline/aggTrade REST endpoints that could serve this, similar to what
`scripts/rebuild_footprint_history.py` does for Bybit/BTCUSDT) — that doesn't exist today.

---

## Stopping everything

**Fast path — just stop the grid brain, leave MT5/terminal running** (what you want most
days, e.g. end of a trading session):

```bash
cd /Users/aniteksachan/Strategies/FootprintBiot-jul09
# 1. ALWAYS check exposure first — never stop with real positions unmonitored
venv/bin/python -c "
from execution.mt5_direct import get_client
c=get_client()
p=[x for x in c.positions('XAUUSD.pc') if 776000<=x['magic']<776140]
d=[x for x in c.pendings('XAUUSD.pc') if 776000<=x['magic']<776140]
print('pos',len(p),'pend',len(d))"
# if non-zero, decide first: flatten manually, or restart is safer than staying stopped
# (arm state persists to disk and restores automatically on the next start — see below)

# 2. stop
kill "$(lsof -ti:5002 -sTCP:LISTEN)"
```

**Full stack teardown** (also stop the broker bridge and, if you want, the terminal itself):

```bash
kill "$(lsof -ti:5002 -sTCP:LISTEN)"     # Flask server
kill "$(lsof -ti:18812 -sTCP:LISTEN)"    # mt5_direct bridge
# MT5 terminal itself — only if you're done for the day AND confirmed flat. Note:
# "MetaTrader 5" is a macOS Login Item, so it can relaunch on next login unless removed
# via System Settings → General → Login Items.
```

**Emergency flatten** (cancel every jul09 pending + close every jul09 position, scoped by
magic range so it never touches another branch's positions) — lives in the **main repo**,
not this worktree:

```bash
cd /Users/aniteksachan/Strategies/FootprintBiot
venv/bin/python scripts/emergency_cancel_jul09.py
```

---

## Restarting (e.g. after a config or code change)

Arm state (`data/arm_state.jsonl`) persists to disk and is reloaded on every startup, so a
restart with **open positions is safe** — the server re-adopts live cycles rather than
orphaning them. Still, always check exposure first (same snippet as above) so you know what
you're restarting into.

```bash
cd /Users/aniteksachan/Strategies/FootprintBiot-jul09
lsof -ti:5002 -sTCP:LISTEN | xargs -r kill
sleep 2
FLASK_PORT=5002 FB_MAGIC_BASE=776000 venv/bin/python -m server.app > /tmp/jul09_server.log 2>&1 &
disown
# poll until listening (VP precompute takes ~30-60s)
until lsof -nP -iTCP:5002 -sTCP:LISTEN >/dev/null 2>&1; do sleep 4; done
grep "arm state restored" /tmp/jul09_server.log
```

If `mt5_direct` itself has died (check: does `get_client().account_info()` hang or error?),
restart the bridge too, in the same order as a fresh start (bridge before Flask).

---

## Key config — `config/settings.yaml` under `grid_levels:` / `monitor:`

| Knob | Current | What it does |
|---|---|---|
| `cycle_net_target_usd` | 1000 | per-cycle basket profit target |
| `combined_net_target_usd` | 5000 | account-wide floating-profit flatten-everything target |
| `sidefull_trail_activate_usd` / `_giveback_pct` | 150 / 40% | side-completion trail: arms once one side fully fills, flattens on a 40% giveback from peak — needs peak ≥ $150 first |
| `bias_trail_activate_usd` / `_giveback_pct` | 250 / 40% | cycle-level trail: books 50% of the winning side + BE runner, doesn't flatten |
| `hvn_max_legs_by_tf` | `{1m:4, 5m:5, 15m:6, 1h:7}` | ladder leg count per TF |
| `min_step_price_by_tf` | `{1m: 1.0}` | minimum $ spacing between legs (1m only) |
| `touch_arm_confirm_ticks` | 0.2 | tick-reversal required after a touch before arming |
| `vp_min_bars` | 30 | minimum bars before a session's VP is trusted (below this, degenerates into one blob and kills every trigger) |
| `broker_magic_fallback_enabled` / `_interval_s` | true / 2.0 | the broker-truth polling described above |
| `peak_audit_enabled` / `_interval_s` | true / 15 | independent trail-peak watchdog (see below) |

## Monitoring — what's watching itself

Two background watchdogs start automatically with the Flask server (both log-only, never
mutate live state):

- **`[gap-monitor]`** (`pipeline/feed_monitor.py`) — alerts if footprint bar ingestion stalls.
- **`[peak-audit]`** (`execution/peak_audit.py`) — independently re-derives each active
  cycle's trail peak from a fresh broker query every 15s and cross-checks it against what
  `monitor_cycle` recorded. Flags a loud `*** PEAK DIVERGENCE ***` if they disagree by more
  than `max($20, 5%)` for two consecutive checks. This exists because a real incident showed
  a cycle's recorded peak at $128 when the true peak (reconstructed from broker data) was
  $1,018.75 — the root cause was fixed, this is the ongoing check that it stays fixed.

Grep both patterns after any restart, and periodically during a live session:

```bash
grep -E "BROKER-TRUTH FALLBACK DOWN|PEAK DIVERGENCE|gap-monitor.*stale" /tmp/jul09_server.log
```

## Known open item

The EA's chart-saved `InpMagic`/`InpMagicRange` inputs have drifted from the correct values
(`776000` / `150`) at least twice this session, causing the EA to silently under-report or
omit magics entirely. The broker-truth polling above covers for this, but it's a bridge
dependency, not a fix — the actual fix is verifying the Inputs tab on the live chart and
detaching/reattaching (a recompile alone does not reset saved chart inputs).
