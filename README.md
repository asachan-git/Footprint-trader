# FootprintBiot

> Living architecture doc. Updated each turn. Scan top-down. Implementation Log at bottom.

---

## 1. Overview (Session + HTF bias wired — May 2026)

**Latest (session + filter):**
- `pipeline/features/session.py` — Asia/London/Overlap/NY/Off detection + daily 20-SMA HTF bias
- `prompts/builder.py` — `session` + `htf_bias` in every variable_suffix
- `server/routes/decide.py` — `active_hours_utc` time filter; set `"07:00-21:00"` in decide_filter config to block Asian session
- 25/25 tests pass. **Now collecting paper data. Next: Phase 5 at 50+ outcomes.**

## 1. Overview (Phase 2a complete — May 2026)

**What changed in Phase 2a:**
- `llm/schema.py` — Decision extended: `grid_leg`, `parent_position_id`, `add_to_existing`, `invalidation_note`
- `execution/grid_manager.py` — `should_add_leg()` checks footprint before any leg N+1; prevents martingale. `active_grid_summary()` for prompt context.
- `execution/sl_manager.py` — break-even after +1R, trail SL to bar lows/highs after +2R. Called on every bar ingest.
- `prompts/system/v2.txt` — grid-aware + VP interpretation rules + session + HTF bias. Now active.
- `server/routes/decide.py` — passes active grid context + `grid_leg_signal` to Claude on every decision call.
- `server/routes/ingest.py` — calls `sl_manager.check_sl_adjustments()` on every bar.
- 25/25 tests pass.

## 1. Overview (Phase 1 complete — May 2026)

**What changed in Phase 1:**
- `execution/position_store.py` — persisted position state (JSONL). Survives restarts. Tracks grid legs, avg entry, SL/TP, realized R.
- `pipeline/features/invalidation.py` — footprint invalidation: exits position when opposite-side absorption forms AT entry zone (smarter than price-based SL).
- `server/routes/ingest.py` — checks open positions on EVERY bar: SL hit → close, TP absorption → close, invalidation → close immediately. Daily DD circuit breaker halts trading.
- `execution/paper.py` — paper executor now delegates to position_store. One position per symbol at a time.
- `llm/validator.py` — rationale quality gate: reject decisions with <20-char rationale.
- `prompts/builder.py` — CVD 5-bar momentum added to variable suffix.
- `config/settings.yaml` — `min_confidence: 0.60` gate added; `active_hours_utc` time filter.
- `scripts/start.sh` — XAUTUSDT price_step fixed 0.01 → 0.1.
- 25/25 tests pass.

## 1. Overview

Claude-driven footprint trading bot. Tick stream → bar-level footprint → on-demand Claude decision → executor.

- Data: **Bybit BTCUSDT** (free public WS, real footprint) or **Exness XAUUSDm via MetaApi** (paid bridge, quote-only)
- Pipeline: ingress → bar accumulator → Flask `/ingest` → state_store → on `/decide`: features → Claude tool_use → validator → executor
- Cost gate: pre-filter on `/decide` skips Claude when no setup; `/ingest` always free
- Learning: `/label` walks forward, `/stats` aggregates hit rate + expectancy

## 2. Architecture diagram

```
[Bybit WS / Exness MetaApi]
  → ingress module (footprint_builder)
  → POST /ingest          (store bar; no Claude)
  ↑
auto_decide.sh ─ every 60s
  → POST /decide          (pre-filter; if pass → Claude tool_use Decision)
  → executor.dispatch     (journal | paper | live)
  → POST /label           (walk forward, attach outcome)
  → GET /stats            (hit rate / expectancy)
                                                         │
                                                         ▼
                                            [ pipeline ]
                                            normalize → footprint → features → MTF
                                                         │
                                                         ▼
                                            [ prompts/builder ]
                                            cached prefix + variable suffix
                                                         │
                                                         ▼
                                            [ llm/client ]
                                            Anthropic SDK · tool_use · cache_control
                                                         │
                                                         ▼
                                            [ llm/validator ]
                                            SL/TP/RR gates
                                                         │
                                                         ▼
                                            [ execution/router ]
                                            mode enum → journal | paper | live
                                                         │
                                                         ▼
                                            [ backtest/walk_forward ]  →  [ eval/prompt_ab ]
                                            outcome label              vN vs vN+1 report
```

## 3. Folder map

```
FootprintBiot/
├── README.md                   ← you are here (living doc)
├── requirements.txt
├── .env.example
├── venv/                       (gitignored)
├── config/
│   ├── settings.yaml           # mode enum, symbol, tf list, claude model, budgets
│   └── risk.yaml               # max R/trade, daily DD cap, R:R floor
├── spikes/                     # Phase 0 ingress feasibility artifacts
├── lipi/                       # Path A: Lipi indicator (orderflow aggregates)
├── browser_bridge/             # Path B: userscript (footprint ladder enrichment)
├── server/                     # Flask app + routes
├── pipeline/                   # bar normalize → footprint → features → MTF
├── prompts/                    # versioned system prompts + builder
├── llm/                        # Anthropic client, schema, validator, logger
├── execution/                  # journal / paper / live executors + router
├── backtest/                   # walk-forward outcome labeler + dataset builder
├── eval/                       # prompt A/B + reports
├── data/                       # raw payloads, footprint frames, decisions, outcomes, datasets
├── logs/
├── scripts/                    # run_server, run_replay, tune_prompt, check_readme_synced
└── tests/
```

## 4. Data flow (one bar, hybrid path)

1. **Lipi side** — on bar close, Lipi indicator fires `alert(json_string)` with orderflow aggregates. GoCharting POSTs body to webhook URL (Cloudflare tunnel → Flask `/ingest`). Body is plain text — we encode JSON ourselves.
2. **Userscript side (when tab open)** — WS/fetch hook captures footprint ladder frame, POSTs to Flask `/ingest`. Same `bar_id` schema.
3. **Flask `/ingest`** — discriminates on `format` field. Routes to `pipeline/parsers/lipi_v1.py` or `userscript_v1.py`. Per-`bar_id` lock prevents duplicate Claude calls on webhook retry. State store upserts: if both arrive for same `bar_id`, aggregates from Lipi + ladder from userscript merge into single canonical Bar.
4. **Pipeline** — builds footprint matrix from ladder (when present), derives features. If only aggregates available, features degrade gracefully (delta from Lipi, no per-level imbalance).
5. **MTF store** — keyed `(symbol, tf, close_ts)`. Higher-TF context as-of bar close.
6. **Prompt builder** — cached prefix (system + rules + few-shot) + variable suffix (recent N bars + features). `cache_control` on prefix.
7. **LLM** — `tool_use` forces structured `Decision`.
8. **Validator** — SL/TP/RR gates.
9. **Logger** — appends to `data/decisions.jsonl`.
10. **Executor** — `execution/router.py` dispatches by mode enum.
11. **Outcome** — `backtest/walk_forward.py` labels forward bars.

## 5. Setup

```bash
git init
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill in ANTHROPIC_API_KEY
```

Bybit ingress (active):
- No signup, no API keys. Bybit `publicTrade` stream is anonymous.
- Run: `python3 -m bybit.main --symbol BTCUSDT --tf 1m --price-step 1.0`
- This connects to `wss://stream.bybit.com/v5/public/linear`, accumulates ticks per bar, POSTs `bybit_v1` payload to Flask `/ingest` on every bar close.
- `--price-step` rounds prices into cells (1.0 = $1 cells for BTC; tune per instrument).

## 6. Run

```bash
# Terminal 1 — Flask
source venv/bin/activate && PYTHONPATH=. bash scripts/run_server.sh

# Terminal 2 — Bybit ingress (free, BTC)
PYTHONPATH=. python3 -m bybit.main --symbol BTCUSDT --tf 1m --price-step 1.0
# OR Exness ingress (XAUUSD via MetaApi)
PYTHONPATH=. python3 -m exness.main --symbol XAUUSDm --tf 1m --price-step 0.1

# Terminal 3 — auto trigger /decide + /label every minute
bash scripts/auto_decide.sh --symbol BTCUSDT --tf 1m

# Anytime — see metrics
curl -s http://localhost:5000/stats | python3 -m json.tool
```

## 7. Modes

| Mode      | Behavior                                            | Tripwire             |
|-----------|-----------------------------------------------------|----------------------|
| `journal` | Log decision + reasoning. No fills.                 | none                 |
| `paper`   | Simulated fills, in-process PnL.                    | none                 |
| `live`    | Send order to broker.                               | env `ALLOW_LIVE=1`   |

Mode set in `config/settings.yaml`. Live mode refuses to start without `ALLOW_LIVE=1` even if config says `live`.

## 8. Cost & latency budget

| Metric            | Target                              | Notes                                              |
|-------------------|-------------------------------------|----------------------------------------------------|
| Tokens in         | ≤ 8k cached + ≤ 2k variable / call  | cache hit ≥ 90%                                    |
| Tokens out        | ≤ 600 (tool_use Decision)           |                                                    |
| p50 latency       | ≤ 3s                                | Sonnet 4.6 default                                 |
| p99 latency       | ≤ 8s                                | hard timeout → no-trade                            |
| $ per decision    | ≤ $0.02 cached                      | revisit after first replay session                 |

Default model: `claude-sonnet-4-6`. Escalate to `claude-opus-4-7` only on validator-flagged ambiguity.

## 9. Prompt versions

| Version | Date       | Rationale |
|---------|------------|-----------|
| v1      | 2026-05-10 | Initial system prompt — orderflow-focused, single TF. |

## 10. Tunnel (Cloudflare quick tunnel)

GoCharting webhook fires from Cloudflare-hosted servers; can't reach laptop's localhost directly. Cloudflare quick tunnel exposes local port over public HTTPS, no signup.

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:5001       # spike server
cloudflared tunnel --url http://localhost:5000       # production Flask
```

Output prints `https://<random-words>.trycloudflare.com`. Use that URL + `/ingest` (or `/spike_ingest`) in GoCharting alert webhook config.

**Current tunnel URL (rotates per `cloudflared` restart):**
- Spike: `https://wesley-practitioners-hottest-budgets.trycloudflare.com` (rotates)
- Production: TBD

**Verify tunnel reaches server:**
```bash
curl -X POST https://<tunnel>.trycloudflare.com/spike_ingest \
  -H "Content-Type: application/json" \
  -d '{"format":"test","msg":"hello from curl"}'
```

## 11. Lipi syntax notes (discovered via Phase 0 spike)

| Construct | Result | Note |
|-----------|--------|------|
| `//@version=1` + `indicator(...)` + `plot(close)` | ✓ compiles | Baseline. |
| `x = 42` | ✓ | Simple assignment works. |
| `"a" + "b"` (single line) | ✓ | String concat with `+`. |
| `"a"` newline `+ "b"` | ✗ | "no viable alternative at input '\r\n+'" — multi-line `+` forbidden. Always single-line. |
| `footprint.*` | ✗ | "No variable with name `footprint` found." Namespace doesn't exist. |
| `orderflow.*` | ✓ | Real namespace. |
| `orderflow.delta` (no parens) | ✓ | Series variable, not function. |
| `tostring(x)` | ✗ | Not found. |
| `str(x)` | ✗ | Not found. |
| `string(x)` | ✗ | Exists but expects `const string`, not converter. |
| `alert(fmt, args)` | ✗ | "Too many arguments. Expected 1." Not variadic. |
| `alert(series_string)` | ✓ | Per docs: signature `alert(series string message) → void`. Dynamic strings work. |
| `str.*` | ✗ no converter | Autocomplete: `contains, endswith, format_time, length, lower, match, pos, repeat, replace, replace_all, startswith, substring, tonumber`. **String manipulation only — no number→string member.** |

**Open syntax question:** how to convert numeric to string.
- `str.*` namespace: string-manipulation only — no num→string member
- `math.*` namespace: pure math (abs, ceil, floor, log, max, min, pi, pow, round, sin, sqrt, sum, todegrees, toradians, …) — no string conversion
- `int(value)` cast exists with +3 overloads — numeric truncate/parse, not num→string
- `alert(series string message) → void` — accepts series strings, but no obvious way to build one with numerics in it
- `alertcondition(series bool condition, const string title, const string message) → void` — `const string` message, no template syntax in docs

**Most likely remaining path: Alert Widget UI templating.** Pine/TV pattern: script `plot()`s named outputs + alert message field in UI supports `{{plot_name}}` placeholders interpolated by GoCharting at fire time. Probe by typing `{{` inside alert UI message field — if autocomplete pops with placeholder options, this is the path.

If no templating either → **Plan D: drop Lipi as data carrier; use Lipi only as trigger (constant message), userscript carries all data via WS hook.**

## 12. Lipi orderflow API (confirmed from official docs)

Available per-bar series variables (no per-price ladder):

| Variable | Type | Description |
|----------|------|-------------|
| `orderflow.trades` | int | Total trade count in bar |
| `orderflow.buy` | int | Aggressive buy trade count |
| `orderflow.sell` | int | Aggressive sell trade count |
| `orderflow.buyvolume` | num | Total buy volume |
| `orderflow.sellvolume` | num | Total sell volume |
| `orderflow.delta` | num | buyvolume − sellvolume |
| `orderflow.maxdelta` | num | Peak intrabar buy pressure |
| `orderflow.mindelta` | num | Peak intrabar sell pressure |
| `orderflow.cothigh` | num | Cumulative delta from high / retest |
| `orderflow.cotlow` | num | Cumulative delta from low / retest |
| `orderflow.cvd` | (cube icon) | Cumulative volume delta — autocomplete only, type unconfirmed |

**Not exposed in Lipi:** per-price-level bid/ask volume (footprint ladder), POC, value area, imbalance. → Footprint matrix unreachable via Lipi → drives hybrid architecture (userscript covers ladder).

## 13. Webhook delivery model (confirmed from Phase 0)

- GoCharting POSTs `Content-Type: text/plain` to webhook URL
- Body = exact literal string passed to `alert(...)` — no wrapping envelope, no templating
- We own the body format completely → encode JSON in Lipi, POST as text/plain, server parses
- Spike payload `{ "raw_body": "hello world from lipi", "Content-Length": "21", ... }` confirms verbatim delivery
- Replay-mode alert behavior: not yet confirmed (TODO once full payload works)

## 14. Implementation Log

### 2026-05-10 — Initial scaffold
- 82 files, 5-node architecture, 13/13 tests pass
- Path A (Lipi) primary, Path B (userscript) fallback per plan v1
- Spike harness ready (spike_server.py, spike_emit.lipi probe, hook_skeleton.user.js)

### 2026-05-11 — Pre-Phase-0 hardening (from advisor review)
- State store now persists `data/footprint/<symbol>_<tf>.jsonl`, reloads on startup
- MTF aggregation wired into `/ingest`
- Per-`bar_id` lock prevents duplicate Claude calls on webhook retry
- Userscript fetch hook gated by `URL_HINTS`

### 2026-05-18 — Auto-trigger + outcome labeling + stats
- `scripts/auto_decide.sh` — every 60s + 10s offset: POST /decide + POST /label
- `/label` route — walks forward over un-scored non-flat decisions; writes outcome JSONL
- `/stats` route — aggregates: decisions by side, scored vs pending, win rate, expectancy, sum R, avg MFE/MAE
- Layer 5 dispatch was already wired (in /decide → executor.dispatch on non-flat)
- README rewritten in brief one-liner form per section

### 2026-05-18 — Full pipeline e2e working (Bybit + /decide + Claude)
- Bybit BTCUSDT WS → /ingest → state_store: 408+ bars stored
- /decide call returns valid `Decision` from Claude (flat, conf 0.35 — quiet market)
- Pre-filter (`min_abs_delta: 10`) gates Claude calls; `force:true` bypasses
- timeout_s bumped 8 → 30 (cold cache call took ~8-10s)
- recent_bars trimmed 20 → 10 to reduce prompt size + latency
- v1 system prompt updated to require rationale on every decision (was being silently dropped)

### 2026-05-18 — Cost control: pre-filter on /decide
- User flagged rising Anthropic API cost
- `/decide` now runs local feature check first (delta threshold, optional stacked imbalance / absorption gates)
- Skips Claude call when no candidate setup; returns `{"ok":true,"skipped":true,"reason":...}`
- Override with `{"force": true}` in body
- Configured in `settings.yaml::decide_filter` — defaults to `min_abs_delta: 10.0` (tune per symbol)
- /ingest remains free (storage only, no Claude)

### 2026-05-18 — XAUUSDm live ingress proved (Exness + MetaApi)
- Capital.com blocked in India → reverted to Exness path
- MetaApi $10/mo paid → account deployed → tick stream confirmed
- Exact symbol on Exness server: **`XAUUSDm`** (suffix `m` for mini account)
- Exness ticks are **quote-only** (`last=None, flags=0`) → no native aggressor side
- Footprint builder updated: when `last` is None, fall back to **mid-price tick rule** (uptick=buyer, downtick=seller)
- `exness/list_symbols.py` diagnostic tool added — lists available symbols on connected MT5 account
- MetaApi SDK 29.x import path: `from metaapi_cloud_sdk import MetaApi, SynchronizationListener`
- `dotenv.load_dotenv()` added to exness/main.py
- SDK INFO logs silenced to clean up output
- Waiting on market open for first stored bars + /decide test

### 2026-05-18 — Capital.com ingress (India-friendly, no balance gate)
- MetaApi.cloud blocked on $10-20 balance requirement → switched broker
- Capital.com demo accepts India, free, has Gold CFD, Mac-native via Python
- New: `capital/auth.py` (REST session login → CST + X-SECURITY-TOKEN), `capital/ws_client.py` (WS market data subscriber), `capital/footprint_builder.py` (tick rule: mid uptick=buyer, downtick=seller)
- Native trade tape unavailable (CFD broker) → pseudo-footprint via quote-rule aggressor inference
- New: `pipeline/parsers/capital_v1.py`; normalizer dispatches it
- Env: `CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_PASSWORD`
- 21/21 tests pass (new: capital_v1 parser, tick-rule footprint builder)
- Bybit (crypto BTC) + Exness (parked) + Capital.com all share canonical Bar via different parsers

### 2026-05-17 — Exness MT5 ingress via MetaApi.cloud (India-accessible XAUUSD)
- User in India → OANDA blocked. Exness already on hand → MetaApi.cloud bridges MT5 to Mac Python.
- New: `exness/ws_client.py` (MetaApi streaming connection), `exness/footprint_builder.py` (MT5 tick flags → aggressor, Lee-Ready fallback), `exness/main.py`
- Native aggressor: MT5 `flags & TICK_FLAG_BUY/SELL` exposes taker side per trade tick
- New: `pipeline/parsers/exness_v1.py`; normalizer dispatches it
- Env: `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID`, `METAAPI_REGION`
- 19/19 tests pass (new: exness_v1 parser, infer_side flags+Lee-Ready, builder boundary emit)
- Bybit kept active; both ingress paths supported simultaneously

### 2026-05-15 — Claude decoupled from /ingest. Decision now on-demand.
- Every-bar Claude call was wasteful (~$14-72/day per symbol)
- **/ingest** now only stores bar + computes features (cheap, fast, no LLM)
- **/decide** new endpoint: pulls latest bars from state_store + invokes Claude on demand
- Body: `{"symbol":"BTCUSDT","tf":"1m"}` (both optional, fall back to settings)
- Returns decision + dispatch result
- Future: pre-filter (only call /decide when local features signal a setup), or scheduled trigger
- 15/15 tests pass

### 2026-05-15 — Bybit WebSocket realtime data ingress live
- User chose realtime tick data over screenshot vision
- Picked **Bybit BTCUSDT perpetual**: free public WS, native taker side per trade, real footprint signal, zero auth
- Built `bybit/ws_client.py` (async WS subscriber), `bybit/footprint_builder.py` (tick → bar accumulator with price-step cell bucketing), `bybit/main.py` (entry point)
- Added `pipeline/parsers/bybit_v1.py`; normalizer dispatches it
- `Level.vol`, `Cell.bid_vol/ask_vol`, `Bar.delta` now `float` (was `int`) to handle fractional crypto sizes
- 15/15 tests pass (new: bybit_v1 parser test + footprint_builder accumulator test)
- Production path on COMEX GC (Databento) deferred until Bybit MVP proves loop

### 2026-05-15 — Layered architecture defined + Layer 0 auto-loop built
- 7 layers defined (0 Data, 1 Ingestion, 2 Features, 3 Prompt, 4 Decision, 5 Execution, 6 Learning)
- Vision flow end-to-end working (Decision returned successfully, rationale now defensively defaulted)
- New: `scripts/auto_analyze.sh` — capture screen on bar-close cadence, dedup by hash, log decisions
- Open: Layer 5 dispatch from /analyze, Layer 6 outcome tracking, Layer 0 region cropping

### 2026-05-14 — Screenshot → Claude Vision as primary MVP path
- ATAS: Windows-only, can't run on MacBook Air → parked (FootprintEmitter.cs kept for future Windows use)
- Tradovate: API access blocked → parked
- **Primary path now: GoCharting footprint screenshot → Claude Vision → Decision**
- New: `server/routes/analyze.py` — POST `/analyze` accepts PNG, calls Claude Vision with footprint system prompt
- New: `prompts/system/vision_v1.txt` — footprint image analysis system prompt
- New: `scripts/analyze_screenshot.sh` — Cmd+Shift+4 area select → auto-POSTs to Flask → prints Decision
- All 13 tests passing

### 2026-05-14 — Architecture pivot: ATAS C# → Flask replaces all GoCharting ingress
- ATAS confirmed: full `PriceVolumeInfo` per price (Bid, Ask, Volume) via C# API
- COMEX GC Gold Futures replaces OANDA XAUUSD spot (OANDA has no tick tape)
- `atas/FootprintEmitter.cs` written — reads full footprint on bar close, POSTs `atas_v1` JSON
- `pipeline/parsers/atas_v1.py` added; normalizer updated
- GoCharting, Lipi, userscript, browser_bridge: **all retired from data pipeline**
- Flask `/ingest` now handles `atas_v1` format (ATAS C# webhook) + `userscript_v1` (kept as fallback)

### 2026-05-12–13 — Phase 0 COMPLETE — Lipi dropped, userscript sole ingress

**Lipi exhaustive findings:**
- ✅ Compiles, `orderflow.*` API confirmed (delta, buyvolume, sellvolume, trades, maxdelta, mindelta, cothigh, cotlow)
- ✅ Webhook fires — alert body = literal string passed to `alert()`
- ❌ No numeric→string conversion exists (`tostring`, `str()`, `string(numeric)`, `str.*` — all fail)
- ❌ `alert()` takes exactly 1 const/series string — no format args
- ❌ `alertcondition()` message is `const string` — no dynamic data
- ❌ `str.format_time(time, fmt)` returns format template literally — time var likely wrong type or NA
- ❌ Alert UI message field ignored — script const string always wins, no GoCharting-side templating
- ❌ No "always-on" advantage — Lipi script runs in same browser tab as userscript

**Decision: DROP LIPI from pipeline entirely.** Cannot carry any variable data. No architectural advantage over userscript.

**New ingress: userscript only.** WS/fetch hook in browser tab delivers full footprint data (aggregates + per-price ladder if available) per bar close.

### Open items
- Phase 0b: identify GoCharting WS/XHR frame carrying footprint data via userscript hook
- Determine frame schema (bid/ask ladder per price, OHLC, timestamps)
- Wire `pipeline/parsers/userscript_v1.py` to real frame schema
- Full end-to-end: userscript → Flask → Claude → journal mode decision
