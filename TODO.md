# FootprintBiot — TODO & Suggested Improvements

---

## Active / In Progress

- [ ] Collect 50+ paper trade outcomes and check `/stats` hit rate + expectancy
- [ ] Tune `min_abs_delta` per symbol based on observed delta distribution
- [ ] Verify 15m combined decisions (BTCUSDT + XAUTUSDT) via `logs/decide_multi.log`

---

## Suggested Improvements

### Signal Quality

- [ ] **Better gold data** — XAUTUSDT (Bybit spot) has low volume + crypto premium. Switch to COMEX GC via Databento (~$30/mo) for real futures footprint when ready.
- [ ] **Session context in prompt** — add Asia / London / NY session tag + time-of-day to variable suffix. Claude should avoid low-liquidity windows (00:00–07:00 UTC for gold).
- [ ] **HTF bias** — add daily/weekly trend direction (close vs 20-bar SMA) to prompt context so Claude doesn't fade major trends.
- [ ] **CVD direction** — pass cumulative volume delta over last 5 bars separately to highlight momentum shift vs continuation.
- [ ] **XAUTUSDT tick size** — `price_step=0.01` creates too many sparse price levels. Try `0.1` or `0.5` for cleaner footprint cells.
- [ ] **Tick rule accuracy** — Exness/capital quote-only ticks produce synthetic delta. Only Bybit/COMEX GC give native aggressor side. Weight signal confidence accordingly.

### Decision Quality

- [ ] **Few-shot from real outcomes** — replace synthetic examples in `prompts/few_shot/examples.jsonl` with actual winning setups from `data/decisions.jsonl` + `data/outcomes.jsonl`.
- [ ] **Prompt A/B testing** — after 50+ outcomes, run `python3 scripts/tune_prompt.py --a v1 --b v2 --dataset data/datasets/train_v1.jsonl` to measure which prompt version performs better.
- [ ] **Confidence threshold gate** — add `min_confidence: 0.60` to `config/settings.yaml::decide_filter`. Only dispatch when Claude confidence ≥ threshold.
- [ ] **Time filter** — skip `/decide` calls during specific hours (configurable in settings). Avoid low-volatility windows.
- [ ] **Require rationale quality** — add validator check: reject decision if rationale length < 20 chars (Claude hedging with no reasoning).

### Architecture

- [ ] **Outcome-driven prompt tuning** — after 50+ outcomes, extract top 10 winning trade setups from decisions log, add as new few-shot examples in `prompts/few_shot/examples.jsonl`.
- [ ] **Risk scaling by confidence** — scale paper/live position size proportional to `decision.confidence × historical_win_rate`. High-confidence setups get full R; low-confidence get 0.5R.
- [ ] **Alert on decision** — send push notification (Telegram, ntfy.sh, or macOS notification) when a non-flat decision fires with confidence ≥ threshold.
- [ ] **Dashboard** — simple web UI (Flask + minimal HTML) showing: live footprint, last 5 decisions, running P&L, hit rate. Auto-refresh every 30s.

### Data Sources (Parked — revisit when ready)

- [ ] **Databento COMEX GC** (~$30/mo) — real gold futures tick data, native aggressor side. Best XAUUSD proxy. Mac-native Python SDK.
- [ ] **ATAS C# indicator** (`atas/FootprintEmitter.cs`) — requires Windows. Full per-price `PriceVolumeInfo` footprint. Parked until user has Windows machine.
- [ ] **Exness XAUUSDm via MetaApi** — working but $10/mo bridge. Quote-only ticks = synthetic delta. Keep as backup when gold market open.

### Execution

- [ ] **Bybit order API** (`execution/live/bybit_adapter.py`) — implement REST order placement for BTCUSDT. Only after paper mode proves positive expectancy.
- [ ] **Daily P&L summary** — end-of-day log: trades taken, wins/losses, realized R, remaining open positions.
- [ ] **Max drawdown circuit breaker** — `config/risk.yaml::daily.max_dd_r` already defined; wire it into executor to halt trading when daily DD limit hit.

### Monitoring

- [ ] **Log rotation** — logs grow unbounded. Add `logging.handlers.RotatingFileHandler` or daily log rotation.
- [ ] **Stale stream detector** — alert if no new bar in >5 minutes (connection silently dropped). Add to `scripts/start.sh` or Flask health endpoint.
- [ ] **Bar count endpoint** — `GET /status` returning: bars per symbol+TF, last bar timestamp, connection alive Y/N, decisions today.

---

## Completed

- [x] Bybit BTCUSDT + XAUTUSDT real-time footprint ingress
- [x] Flask `/ingest` (store only, no LLM cost)
- [x] Flask `/decide` with pre-filter (delta threshold)
- [x] Flask `/decide_multi` — BTC + XAUT combined with cross-market note
- [x] Flask `/label` — walk-forward outcome labeler
- [x] Flask `/stats` — hit rate, expectancy, sum R
- [x] Flask `/footprint` — ASCII footprint visualizer
- [x] `scripts/start.sh` — single command launch
- [x] `scripts/reset.sh` — clear all data
- [x] MTF aggregation (1m → 5m → 15m) with delta + POC
- [x] Paper mode executor with rationale + R:R in log
- [x] Journal executor with rationale + R:R in log
- [x] Prompt v1 system prompt requiring rationale on every decision
- [x] `prompts/few_shot/examples.jsonl` with 3 seed examples
