# Phase 0 — Ingress Feasibility Spike

**Goal:** prove one ingress path can deliver a full footprint ladder (bid + ask volume per price level) per bar to the Flask server. Block all downstream work until this is settled.

## Status

| Path | Status | Notes |
|------|--------|-------|
| A — Lipi script | TODO | Requires testing in GoCharting Lipi editor. See `spike_emit.lipi`. |
| A+compression | TODO | Only if A succeeds but alert payload caps below full-ladder size. |
| B — Userscript (WS/fetch hook) | TODO | See `hook_skeleton.user.js`. Fallback if A fails. |
| Canvas-only fallback | not yet attempted | Manual CSV export only. Kills live mode. |

## Phase 0a — Lipi script

Three questions:

1. **Footprint access** — does Lipi expose per-bar bid/ask volume per price level (look for `footprint.bidVolume[price]`, `orderflow.askVolumeAt(price)`, similar)?
2. **Alert payload size** — what's the max JSON payload Lipi `alert()` / webhook can send? A 1-minute NQ footprint can be ~20–100 price levels × 2 = up to ~200 numeric fields.
3. **Replay** — do Lipi alerts fire when user scrubs replay mode?

### How to run

1. Open `spike_emit.lipi`, fill in real Lipi APIs (placeholders marked `// TODO:`).
2. In a separate shell, run `python spike_server.py` (this dir) — minimal Flask catching POSTs to `/spike_ingest`.
3. Paste the Lipi script into GoCharting Lipi editor on a footprint chart (any 1-minute instrument).
4. Configure alert webhook → `http://localhost:5001/spike_ingest`.
5. Let one live bar close. Save received payload to `sample_bar_lipi.json`.
6. Enter replay mode, scrub one bar, confirm second payload arrives.
7. Fill in the result table at the top.

### Decision matrix

| 1 | 2 | 3 | Outcome |
|---|---|---|---------|
| ✅ | ✅ | ✅ | Path A locked. Remove `browser_bridge/` from build. |
| ✅ | partial | ✅ | Path A + compression (see `lipi/compression.md`). |
| ✅ | ✅ | ❌ | Path A for live, Path B for replay. |
| ❌ | — | — | Skip to Phase 0b. |

## Phase 0b — Userscript

Only if 0a insufficient. Drop `hook_skeleton.user.js` into Tampermonkey, open GoCharting, scroll/scrub. Inspect console for frames with per-price `bid_vol`/`ask_vol` arrays. Save sample to `sample_bar.json`.

## Final decision

Record the chosen path and rationale here when the spike concludes:

> _TBD_
