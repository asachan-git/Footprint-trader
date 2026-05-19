using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using ATAS.Indicators;
using ATAS.Indicators.Technical;

// FootprintBiot — ATAS footprint emitter
// Reads full per-price bid/ask footprint from ATAS IndicatorCandle on each bar close.
// POSTs canonical atas_v1 JSON to Flask /ingest.
//
// Build:
//   - Target: .NET Standard 2.0 or .NET 8
//   - Reference: ATAS.DataFeedsCore.dll (from ATAS platform SDK)
//   - Output DLL → place in ATAS indicators folder
//
// Config:
//   - Change FLASK_URL to your Flask server (local or tunnel)
//   - Attach indicator to any GC (Gold Futures) footprint chart in ATAS

namespace FootprintBiot
{
    public class FootprintEmitter : Indicator
    {
        private static readonly HttpClient _http = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(5)
        };

        private const string FLASK_URL = "http://localhost:5000/ingest";

        public FootprintEmitter()
        {
            DenyToChangePanel = true;
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            // Fire only on fully closed bars, not the forming live bar
            if (bar >= CurrentBar - 1) return;

            var candle = GetCandle(bar);
            if (candle == null) return;

            SendAsync(candle, bar).ConfigureAwait(false);
        }

        private async Task SendAsync(IndicatorCandle candle, int bar)
        {
            try
            {
                var bidLadder = new List<object>();
                var askLadder = new List<object>();

                foreach (var level in candle.GetAllPriceLevels())
                {
                    if (level == null || level.Volume == 0) continue;
                    var price = (double)level.Price;
                    bidLadder.Add(new { price, vol = (double)level.Bid });
                    askLadder.Add(new { price, vol = (double)level.Ask });
                }

                var closedTs = ((DateTimeOffset)candle.LastTime).ToUnixTimeSeconds();
                var barId = $"{InstrumentInfo?.Instrument}|{TimeFrameDescr}|{closedTs}";

                var poc = candle.MaxVolumePriceInfo;

                var payload = new
                {
                    format = "atas_v1",
                    source = "live",
                    bar_id = barId,
                    symbol = InstrumentInfo?.Instrument ?? "GC",
                    tf = TimeFrameDescr,
                    close_ts = closedTs,
                    ohlc = new
                    {
                        o = (double)candle.Open,
                        h = (double)candle.High,
                        l = (double)candle.Low,
                        c = (double)candle.Close
                    },
                    bid_ladder = bidLadder,
                    ask_ladder = askLadder,
                    delta = (double)candle.Delta,
                    poc = poc != null ? (double?)poc.Price : null,
                    buyvolume = (double)candle.Ask,
                    sellvolume = (double)candle.Bid,
                    maxdelta = (double)candle.MaxDelta,
                    mindelta = (double)candle.MinDelta,
                    trades = candle.Ticks
                };

                var json = JsonSerializer.Serialize(payload);
                var content = new StringContent(json, Encoding.UTF8, "application/json");
                await _http.PostAsync(FLASK_URL, content);
            }
            catch (Exception ex)
            {
                // Don't crash indicator on network error
                AddAlert($"[FootprintBiot] send failed: {ex.Message}");
            }
        }
    }
}
