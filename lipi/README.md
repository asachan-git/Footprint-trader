# Path A — GoCharting Lipi indicator

Production version of the per-bar footprint emitter. Built **only after** Phase 0a spike confirms Lipi exposes footprint cells + alert webhook can carry the payload.

Files:
- `footprint_emit.lipi` — production indicator; emits canonical `lipi_v1` payload on each bar close.
- `compression.md` — payload-size strategy (delta-encoded ladder, base price + offsets) if alerts are size-capped.

Webhook target (configure in GoCharting alert settings): `http://<flask-host>:5000/ingest`

Canonical payload shape (matches `pipeline/parsers/lipi_v1.py`):

```json
{
  "format": "lipi_v1",
  "source": "live | replay",
  "bar_id": "<sha1 of symbol|tf|close_ts>",
  "symbol": "NQ",
  "tf": "1m",
  "close_ts": 1715342400,
  "ohlc": {"o": 0, "h": 0, "l": 0, "c": 0},
  "bid_ladder": [{"price": 0.0, "vol": 0}],
  "ask_ladder": [{"price": 0.0, "vol": 0}],
  "poc": 0.0,
  "delta": 0
}
```
