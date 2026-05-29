import { useEffect, useRef } from "react";

const C = {
  bg:   "#0d0d0d",
  poc:  "#ffd700",
  vah:  "#00bcd4",
  val:  "#e040fb",
  hvn:  "#42a5f5",
  lvn:  "#ff9800",
  bid:  "rgba(38,166,154,0.7)",
  ask:  "rgba(239,83,80,0.7)",
  text: "#555",
};

export default function VolumeProfile({ dailyVp, bars }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const W = canvas.width;
    const H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, W, H);

    if (!dailyVp || !dailyVp.poc) return;

    // Price range from bars
    const prices = bars.flatMap(b => [b.h, b.l]);
    const priceMin = prices.length ? Math.min(...prices) : (dailyVp.val || 0) * 0.99;
    const priceMax = prices.length ? Math.max(...prices) : (dailyVp.vah || 0) * 1.01;
    const priceRange = priceMax - priceMin || 1;

    function priceToY(p) {
      return H - ((p - priceMin) / priceRange) * H;
    }

    // Aggregate volume per price level from bars
    const step = (dailyVp.poc || 1) * 0.0001;  // ~0.01% price step
    const buckets = {};
    bars.forEach(b => {
      const bucket = Math.round(b.c / step) * step;
      const k = bucket.toFixed(4);
      if (!buckets[k]) buckets[k] = { price: bucket, bid: 0, ask: 0 };
      buckets[k].bid += b.bid_vol || 0;
      buckets[k].ask += b.ask_vol || 0;
    });

    const rows = Object.values(buckets).sort((a, b) => a.price - b.price);
    const maxVol = rows.reduce((m, r) => Math.max(m, r.bid + r.ask), 0) || 1;

    // Draw bars
    const barH = Math.max(1, H / rows.length);
    rows.forEach(r => {
      const y = priceToY(r.price);
      const totalW = ((r.bid + r.ask) / maxVol) * (W - 14);
      const bidW  = (r.bid / (r.bid + r.ask || 1)) * totalW;
      const askW  = totalW - bidW;

      // bid (left)
      ctx.fillStyle = C.bid;
      ctx.fillRect(0, y - barH / 2, bidW, barH);
      // ask (right of bid)
      ctx.fillStyle = C.ask;
      ctx.fillRect(bidW, y - barH / 2, askW, barH);
    });

    // HVN zones
    (dailyVp.hvn_zones || []).forEach(z => {
      const y1 = priceToY(z.high);
      const y2 = priceToY(z.low);
      ctx.fillStyle = "rgba(66,165,245,0.12)";
      ctx.fillRect(0, y1, W, y2 - y1);
    });

    // LVN zones
    (dailyVp.lvn_zones || []).forEach(z => {
      const y1 = priceToY(z.high);
      const y2 = priceToY(z.low);
      ctx.fillStyle = "rgba(255,152,0,0.10)";
      ctx.fillRect(0, y1, W, y2 - y1);
    });

    // Key level lines + labels
    const levels = [
      { price: dailyVp.poc, color: C.poc, label: "POC" },
      { price: dailyVp.vah, color: C.vah, label: "VAH" },
      { price: dailyVp.val, color: C.val, label: "VAL" },
    ];
    levels.forEach(({ price, color, label }) => {
      if (!price) return;
      const y = priceToY(price);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(W, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.font = "9px monospace";
      ctx.fillText(label, W - 26, y - 2);
    });

  }, [dailyVp, bars]);

  return (
    <canvas
      ref={canvasRef}
      width={120}
      style={{ width: "100%", height: "100%", display: "block" }}
      height={window.innerHeight * 0.6 || 400}
    />
  );
}
