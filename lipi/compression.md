# Lipi payload compression strategy

Only used if Phase 0a spike shows alert webhook payload cap is below the size needed for a full footprint ladder.

## Full ladder size estimate

- 1-min NQ: 20–60 price ticks active per bar.
- Per level: `{"price": 17234.25, "vol": 1234}` ≈ 30 bytes.
- 2 ladders × 60 × 30 = ~3.6 KB JSON.

If alert cap is e.g. 1 KB, need to shrink ~3–4×.

## Compression scheme: base price + offset array

Replace each level `{price, vol}` with `[offset, vol]` where `offset = (price - base_price) / tick_size`.

Before:
```json
"bid_ladder": [
  {"price": 17234.00, "vol": 12},
  {"price": 17234.25, "vol": 45},
  {"price": 17234.50, "vol": 33}
]
```

After:
```json
"base_price": 17234.00,
"tick_size": 0.25,
"bid_offsets": [[0, 12], [1, 45], [2, 33]]
```

Saves ~50% on each level. `pipeline/parsers/lipi_v1.py` decompresses transparently when `base_price` + `tick_size` keys present.

## Further options (only if still too large)

1. Bit-pack offsets into a base64 string.
2. Drop zero-vol levels (already implicit).
3. Split into two alerts (`bid_part`, `ask_part`) keyed by `bar_id` — server merges on second arrival. Fragile; last resort.

Decide which scheme is needed only after the spike measures the actual cap.
