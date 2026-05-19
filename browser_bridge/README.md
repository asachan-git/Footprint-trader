# Path B — Browser bridge (fallback)

Used only when Lipi path (`lipi/`) can't deliver full footprint data. Userscript hooks GoCharting's WebSocket / fetch traffic in the browser tab and forwards per-bar footprint payloads to Flask `/ingest`.

Files:
- `userscript/gocharting_footprint.user.js` — production userscript. Filters identified-footprint frames, normalizes shape to `userscript_v1`, POSTs to `/ingest`.
- `playwright/scrape.py` — headless variant for CI / unattended runs.

## Userscript install

1. Install Tampermonkey in Chrome.
2. Add new script, paste `userscript/gocharting_footprint.user.js`.
3. Edit constants at top of script — set `FLASK_URL`, identified footprint frame matcher (filled in after Phase 0b spike).
4. Open GoCharting, log in, load footprint chart on target symbol.
5. Verify decisions appear in `data/decisions.jsonl`.

## Canonical payload shape (`userscript_v1`)

```json
{
  "format": "userscript_v1",
  "source": "live | replay",
  "bar_id": "<sha1 of symbol|tf|close_ts>",
  "symbol": "NQ",
  "tf": "1m",
  "close_ts": 1715342400,
  "raw_frame": { ... whatever shape GoCharting actually sends ... }
}
```

The pipeline parser (`pipeline/parsers/userscript_v1.py`) extracts ladder from `raw_frame` based on the schema discovered in Phase 0b.
