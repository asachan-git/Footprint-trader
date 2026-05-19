# ATAS footprint emitter

C# indicator that reads the full per-price footprint from ATAS on each bar close and POSTs canonical `atas_v1` JSON to the Flask server.

## What it provides

ATAS exposes `IndicatorCandle.GetAllPriceLevels()` → `PriceVolumeInfo` per price:
- `Price` — price level
- `Bid` — volume traded at bid at this price  
- `Ask` — volume traded at ask at this price
- `Delta` — Ask − Bid per level

Plus bar-level: Open/High/Low/Close, Delta, MaxDelta, MinDelta, POC (MaxVolumePriceInfo), buy/sell volume totals, trade count.

## Instrument

COMEX GC (Gold Futures). Requires futures data feed: Rithmic, CQG, dxFeed, or Interactive Brokers.

## Build

Requirements:
- Visual Studio 2022 or JetBrains Rider
- .NET 8 SDK
- ATAS SDK DLLs — copy from ATAS installation folder (typically `C:\Program Files\ATAS\bin\`)
  - `ATAS.DataFeedsCore.dll`
  - `ATAS.Indicators.dll`

Steps:
1. Create new Class Library project targeting .NET Standard 2.0 or .NET 8
2. Add reference to ATAS DLLs
3. Add `System.Text.Json` NuGet package
4. Replace `FootprintEmitter.cs` content with file from this repo
5. Update `FLASK_URL` constant (default: `http://localhost:5000/ingest`)
6. Build → Release DLL
7. Copy DLL to ATAS indicators folder: `Documents\ATAS\Indicators\`
8. Restart ATAS → indicator appears in "FootprintBiot" category

## Attach to chart

1. In ATAS, open GC chart (any TF: 1m, 5m, 15m)
2. Chart type: Cluster (footprint) — required for per-price data
3. Add indicator: search "FootprintEmitter"
4. Make sure Flask server is running (`bash scripts/run_server.sh`)
5. Watch logs — each bar close fires one POST to `/ingest`

## Payload schema (`atas_v1`)

```json
{
  "format": "atas_v1",
  "source": "live",
  "bar_id": "GC H25|1m|1747344060",
  "symbol": "GC H25",
  "tf": "1m",
  "close_ts": 1747344060,
  "ohlc": {"o": 3320.5, "h": 3325.0, "l": 3318.0, "c": 3323.5},
  "bid_ladder": [{"price": 3318.0, "vol": 45}, ...],
  "ask_ladder": [{"price": 3318.0, "vol": 12}, ...],
  "delta": 234,
  "poc": 3321.0,
  "buyvolume": 1240.0,
  "sellvolume": 1006.0,
  "maxdelta": 312.0,
  "mindelta": -88.0,
  "trades": 487
}
```

## Local vs tunnel URL

If running Flask on same machine as ATAS (Windows): `http://localhost:5000/ingest`

If Flask on separate machine / Mac: set `FLASK_URL` to your machine's IP or Cloudflare tunnel URL.
