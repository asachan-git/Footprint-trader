import { useEffect, useMemo, useRef, useState } from "react";
import { createChart, CrosshairMode, LineStyle } from "lightweight-charts";

const COLORS = {
  bg:        "#0d0d0d",
  text:      "#666",
  grid:      "#1a1a1a",
  poc:       "#ffd700",
  vah:       "#00bcd4",
  val:       "#e040fb",
  up:        "#26a69a",
  down:      "#ef5350",
  legFilled: "#26a69a",
  tp:        "#ffd700",
  sl:        "#ef5350",
};

const IST = 19800; // UTC+5:30 in seconds

function istFormatter(ts) {
  const d = new Date((ts + IST) * 1000);
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

// ── volume profile overlay ────────────────────────────────────────────────────

function drawVP(canvas, chart, candleSeries, dailyVp, bars) {
  if (!canvas || !chart || !candleSeries || !bars.length) return;

  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);

  const VP_MAX_W = Math.round(W * 0.16);
  const poc = dailyVp?.poc || (bars[0]?.c || 1);
  const step = Math.max(poc * 0.0003, 0.01);

  // Bucket bar volumes by close price
  const buckets = {};
  for (const b of bars) {
    const key = Math.round(b.c / step);
    const k = key.toFixed(0);
    if (!buckets[k]) buckets[k] = { price: key * step, bid: 0, ask: 0 };
    buckets[k].bid += b.bid_vol || 0;
    buckets[k].ask += b.ask_vol || 0;
  }

  const rows = Object.values(buckets);
  const maxVol = rows.reduce((m, r) => Math.max(m, r.bid + r.ask), 0) || 1;

  const hvnZones = dailyVp?.hvn_zones || [];
  const lvnZones = dailyVp?.lvn_zones || [];

  for (const r of rows) {
    const y = candleSeries.priceToCoordinate(r.price);
    if (y === null || y < 0 || y > H) continue;

    const total  = r.bid + r.ask || 1;
    const totalW = (total / maxVol) * VP_MAX_W;
    const bidW   = (r.bid / total) * totalW;
    const askW   = totalW - bidW;
    const cellH  = Math.max(2, (step / (poc * 0.01)) * 0.5);

    // HVN/LVN background tint
    const isHvn = hvnZones.some(z => r.price >= z.low && r.price <= z.high);
    const isLvn = lvnZones.some(z => r.price >= z.low && r.price <= z.high);
    if (isHvn) {
      ctx.fillStyle = "rgba(66,165,245,0.06)";
      ctx.fillRect(0, y - cellH, VP_MAX_W, cellH * 2);
    }
    if (isLvn) {
      ctx.fillStyle = "rgba(255,152,0,0.06)";
      ctx.fillRect(0, y - cellH, VP_MAX_W, cellH * 2);
    }

    const isPoc = dailyVp?.poc && Math.abs(r.price - dailyVp.poc) < step * 0.7;
    ctx.fillStyle = isPoc ? "rgba(255,215,0,0.55)" : "rgba(38,166,154,0.30)";
    ctx.fillRect(0, y - 1, bidW, 2);
    ctx.fillStyle = isPoc ? "rgba(255,215,0,0.55)" : "rgba(239,83,80,0.30)";
    ctx.fillRect(bidW, y - 1, askW, 2);
  }
}

// ── Big-trade bubble overlay ─────────────────────────────────────────────────
function drawBubbles(canvas, chart, candleSeries, bigTrades, show, bars, hitsOut) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;
  ctx.clearRect(0, 0, W, H);
  if (!show || !chart || !candleSeries || !bigTrades?.length || !bars?.length) return;

  const MAX_R = 24, MIN_R = 8;
  // Use logical index → coordinate (more reliable than timeToCoordinate which
  // returns null when chart was hidden / freshly mounted / time gaps exist).
  const sortedBars = [...bars].sort((a, b) => a.ts - b.ts);
  const barTs = sortedBars.map(b => b.ts);
  // Strict visible-only: render bubbles only for bars currently in view.
  const vr = chart.timeScale().getVisibleRange();
  const tFrom = vr?.from ?? barTs[0];
  const tTo   = vr?.to   ?? barTs[barTs.length - 1];
  // Bubble's owning bar = smallest barTs >= bubble.ts (bar that closes at/after ts).
  // Within TF window only. Returns -1 if no visible bar owns this bubble.
  let typTF = Infinity;
  for (let i = 1; i < Math.min(barTs.length, 10); i++) {
    typTF = Math.min(typTF, barTs[i] - barTs[i - 1]);
  }
  if (!Number.isFinite(typTF) || typTF <= 0) typTF = 60;
  const owningLogical = (ts) => {
    let lo = 0, hi = barTs.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (barTs[mid] < ts) lo = mid + 1; else hi = mid;
    }
    if (lo >= barTs.length) {
      // Allow up to 2× TF — XAU has no Binance live bar so events fall past
      // last closed bar between server snapshots.
      const last = barTs[barTs.length - 1];
      return (ts - last) < (typTF * 2) ? barTs.length - 1 : -1;
    }
    return (barTs[lo] - ts) <= typTF ? lo : -1;
  };
  const X_CELL = 28, Y_CELL = MAX_R * 1.6;
  const mergeMap = new Map();
  for (const bt of bigTrades) {
    if (bt.ts < tFrom || bt.ts > tTo + typTF * 2) continue;
    const logical = owningLogical(bt.ts);
    if (logical < 0) continue;
    let x = chart.timeScale().logicalToCoordinate(logical);
    if (x == null) {
      x = chart.timeScale().timeToCoordinate(barTs[logical]);
    }
    if (x == null) continue;
    const y = candleSeries.priceToCoordinate(bt.price);
    if (y == null) continue;
    const key = `${Math.round(x / X_CELL)}|${Math.round(y / Y_CELL)}|${bt.aggressor}`;
    const cur = mergeMap.get(key);
    if (!cur) {
      mergeMap.set(key, { x, y, volume: bt.volume || 0,
        aggressor: bt.aggressor, outcome: bt.outcome, count: 1,
        ts: bt.ts, price: bt.price, barIdx: logical });
    } else {
      cur.volume += bt.volume || 0;
      cur.count += 1;
      cur.x = (cur.x * (cur.count - 1) + x) / cur.count;
      cur.y = (cur.y * (cur.count - 1) + y) / cur.count;
      if (bt.ts > cur.ts) cur.ts = bt.ts;  // newest ts of cluster
      const rank = { pending: 0, absorbed: 1, exhausted: 1, pushed: 2 };
      if ((rank[bt.outcome] || 0) > (rank[cur.outcome] || 0)) cur.outcome = bt.outcome;
    }
  }
  let visible = [...mergeMap.values()];
  if (!visible.length) return;
  // Quintile-based sizing: rank by volume, map to 5 discrete radii.
  const _sorted = [...visible].sort((a, b) => (a.volume || 0) - (b.volume || 0));
  const _quintileR = [MIN_R, 12, 16, 20, MAX_R];
  const _quintileMap = new Map();
  for (let i = 0; i < _sorted.length; i++) {
    const q = Math.min(4, Math.floor((i / Math.max(_sorted.length, 1)) * 5));
    _quintileMap.set(_sorted[i], _quintileR[q]);
  }

  if (hitsOut) hitsOut.length = 0;
  for (const bt of visible) {
    const r = _quintileMap.get(bt) || MIN_R;
    if (hitsOut) hitsOut.push({ x: bt.x, y: bt.y, r, bt });
    const isBuy = bt.aggressor === "buy";
    const baseRgb = isBuy ? "38,166,154" : "239,83,80";
    const grad = ctx.createRadialGradient(bt.x, bt.y, 0, bt.x, bt.y, r);
    grad.addColorStop(0, `rgba(${baseRgb},0.9)`);
    grad.addColorStop(1, `rgba(${baseRgb},0.15)`);
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(bt.x, bt.y, r, 0, Math.PI * 2);
    ctx.fill();
    const outcomeBorder = {
      pushed:    { color: `rgba(${baseRgb},1.0)`,  dash: [] },
      absorbed:  { color: "rgba(255,255,255,0.9)", dash: [3, 3] },
      exhausted: { color: "rgba(140,140,140,0.9)", dash: [1, 2] },
      pending:   { color: "rgba(255,215,0,0.9)",   dash: [4, 2] },
    }[bt.outcome] || { color: `rgba(${baseRgb},0.9)`, dash: [] };
    ctx.save();
    ctx.setLineDash(outcomeBorder.dash);
    ctx.strokeStyle = outcomeBorder.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(bt.x, bt.y, r, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    if (r >= 10) {
      const v = bt.volume || 0;
      const vLbl = v >= 1000 ? (v / 1000).toFixed(1) + "k" : v >= 100 ? v.toFixed(0) : v.toFixed(1);
      const cLbl = bt.count > 1 ? `×${bt.count}` : "";
      ctx.font = `bold ${Math.min(11, Math.max(8, r * 0.5))}px JetBrains Mono, monospace`;
      ctx.fillStyle = "#fff";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      if (cLbl) {
        ctx.fillText(vLbl, bt.x, bt.y - 4);
        ctx.font = `bold ${Math.min(9, Math.max(7, r * 0.38))}px JetBrains Mono, monospace`;
        ctx.fillText(cLbl, bt.x, bt.y + 5);
      } else {
        ctx.fillText(vLbl, bt.x, bt.y);
      }
    }
  }
}

// ── Sierra-style footprint canvas renderer ───────────────────────────────────
//
// Each bar: full-width box, prices listed top→bottom.
// Row layout: [bid_vol]  |  [ask_vol]   (left = red bids, right = green asks)
// Imbalance: ask[p] vs bid[p-1tick] (diagonal) ratio ≥ 3 → green tint on ask
//            bid[p] vs ask[p+1tick] ratio ≥ 3 → red tint on bid
// Stacked imbalance: ≥3 consecutive same-side imbalances → bold border marker.
// POC row: yellow background tint.
// Bar footer: signed delta colored by sign.

const IMBALANCE_RATIO = 3.0;
const STACKED_MIN     = 3;

function drawFootprint(canvas, chart, candleSeries, bars) {
  if (!canvas || !chart || !candleSeries) return false;
  if (!bars.length) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ff0";
    ctx.font = "12px monospace";
    ctx.fillText("FP: no bars", 12, 24);
    return true;
  }
  const haveLadder = bars.some(b => (b.bid_ladder?.length || 0) + (b.ask_ladder?.length || 0) > 0);
  if (!haveLadder) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#ff0";
    ctx.font = "12px monospace";
    ctx.fillText(`FP: bars=${bars.length} but no bid/ask_ladder — server returning empty ladders`, 12, 24);
    return true;
  }

  const parent = canvas.parentElement;
  const cssW = parent?.clientWidth  || 800;
  const cssH = parent?.clientHeight || 400;
  const dpr  = window.devicePixelRatio || 1;
  const W    = Math.round(cssW * dpr);
  const H    = Math.round(cssH * dpr);
  if (canvas.width  !== W) canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;
  canvas.style.width  = cssW + "px";
  canvas.style.height = cssH + "px";

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const timeScale = chart.timeScale();
  const lRange = timeScale.getVisibleLogicalRange();
  if (!lRange) return false;

  const barsVisible = Math.max(1, lRange.to - lRange.from);
  const barPx = Math.max(3, cssW / barsVisible);
  const halfW = Math.max(2, barPx / 2 - 2);

  // Use CSS-pixel space for the rest of the function
  const W_use = cssW;

  let drawnBars = 0;
  let drawnCells = 0;

  for (const bar of bars) {
    const xCenter = timeScale.timeToCoordinate(bar.ts);
    if (xCenter === null || xCenter < -barPx || xCenter > W_use + barPx) continue;
    drawnBars++;

    // Wick from H to L (faint, for shape reference)
    const yHigh = candleSeries.priceToCoordinate(bar.h);
    const yLow  = candleSeries.priceToCoordinate(bar.l);
    if (yHigh !== null && yLow !== null) {
      ctx.strokeStyle = "rgba(120,120,120,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xCenter, yHigh);
      ctx.lineTo(xCenter, yLow);
      ctx.stroke();
    }

    const bidMap = {};
    const askMap = {};
    (bar.bid_ladder || []).forEach(l => { bidMap[l.p] = (bidMap[l.p] || 0) + l.v; });
    (bar.ask_ladder || []).forEach(l => { askMap[l.p] = (askMap[l.p] || 0) + l.v; });

    const prices = [...new Set([...Object.keys(bidMap), ...Object.keys(askMap)])]
      .map(Number).sort((a, b) => a - b);
    if (prices.length === 0) continue;

    // Tick size: smallest positive diff between sorted prices; fallback to price*0.0001.
    let step = 0;
    for (let i = 1; i < prices.length; i++) {
      const d = prices[i] - prices[i - 1];
      if (d > 0 && (step === 0 || d < step)) step = d;
    }
    if (step === 0) step = Math.max(prices[0] * 0.0001, 0.01);

    // Row height in px from price scale — sample multiple anchors for stability
    let cellH = 0;
    for (let i = 0; i < prices.length - 1; i++) {
      const ya = candleSeries.priceToCoordinate(prices[i]);
      const yb = candleSeries.priceToCoordinate(prices[i + 1]);
      if (ya !== null && yb !== null) {
        const h = Math.abs(ya - yb);
        if (h > 0) { cellH = h; break; }
      }
    }
    if (cellH === 0) {
      const yA = candleSeries.priceToCoordinate(prices[0]);
      const yB = candleSeries.priceToCoordinate(prices[0] + step);
      cellH = (yA !== null && yB !== null) ? Math.max(2, Math.abs(yA - yB)) : 12;
    }
    cellH = Math.max(3, cellH);

    const maxVol = prices.reduce(
      (m, p) => Math.max(m, (bidMap[p] || 0) + (askMap[p] || 0)), 0.001,
    );
    const showText = cellH >= 9 && barPx >= 36;
    const cellFont = `${Math.min(9, cellH - 2)}px JetBrains Mono, monospace`;

    // Detect diagonal imbalances per price (ask[p] vs bid[p-step], bid[p] vs ask[p+step])
    const askImb = {};
    const bidImb = {};
    for (const p of prices) {
      const ask     = askMap[p] || 0;
      const bidDiag = bidMap[(p - step).toFixed(8)] ?? bidMap[p - step] ?? 0;
      const bid     = bidMap[p] || 0;
      const askDiag = askMap[(p + step).toFixed(8)] ?? askMap[p + step] ?? 0;
      askImb[p] = ask > 0 && ask >= IMBALANCE_RATIO * Math.max(bidDiag, 0.001);
      bidImb[p] = bid > 0 && bid >= IMBALANCE_RATIO * Math.max(askDiag, 0.001);
    }
    // Stacked imbalance runs (top→down on ask side or down→up on bid side)
    const askStack = new Set();
    const bidStack = new Set();
    for (let i = 0; i < prices.length; i++) {
      // Ascending ask runs
      let j = i;
      while (j < prices.length && askImb[prices[j]]) j++;
      if (j - i >= STACKED_MIN) for (let k = i; k < j; k++) askStack.add(prices[k]);
      i = j === i ? i : j - 1;
    }
    for (let i = 0; i < prices.length; i++) {
      let j = i;
      while (j < prices.length && bidImb[prices[j]]) j++;
      if (j - i >= STACKED_MIN) for (let k = i; k < j; k++) bidStack.add(prices[k]);
      i = j === i ? i : j - 1;
    }

    for (const price of prices) {
      const y = candleSeries.priceToCoordinate(price);
      if (y === null) continue;
      drawnCells++;

      const bid = bidMap[price] || 0;
      const ask = askMap[price] || 0;
      const isPoc = bar.poc && Math.abs(price - bar.poc) < step * 0.6;

      // Solid bid/ask cells, opacity scaled by volume share but with a hard floor
      const bidShare = bid / maxVol;
      const askShare = ask / maxVol;
      if (bid > 0) {
        ctx.fillStyle = `rgba(239,83,80,${0.35 + bidShare * 0.55})`;
        ctx.fillRect(xCenter - halfW, y - cellH / 2, halfW, cellH);
      }
      if (ask > 0) {
        ctx.fillStyle = `rgba(38,166,154,${0.35 + askShare * 0.55})`;
        ctx.fillRect(xCenter, y - cellH / 2, halfW, cellH);
      }
      // Cell grid outline
      ctx.strokeStyle = "rgba(0,0,0,0.5)";
      ctx.lineWidth = 1;
      ctx.strokeRect(xCenter - halfW, y - cellH / 2, halfW * 2, cellH);

      if (isPoc) {
        ctx.fillStyle = "rgba(255,215,0,0.30)";
        ctx.fillRect(xCenter - halfW, y - cellH / 2, halfW * 2, cellH);
        ctx.strokeStyle = "rgba(255,215,0,0.95)";
        ctx.lineWidth = 1.5;
        ctx.strokeRect(xCenter - halfW, y - cellH / 2, halfW * 2, cellH);
      }

      // Imbalance markers
      if (askImb[price]) {
        ctx.fillStyle = askStack.has(price) ? "rgba(38,166,154,0.95)" : "rgba(38,166,154,0.55)";
        ctx.fillRect(xCenter + halfW - 3, y - cellH / 2, 3, cellH);
      }
      if (bidImb[price]) {
        ctx.fillStyle = bidStack.has(price) ? "rgba(239,83,80,0.95)" : "rgba(239,83,80,0.55)";
        ctx.fillRect(xCenter - halfW, y - cellH / 2, 3, cellH);
      }

      if (showText) {
        ctx.font = cellFont;
        ctx.textBaseline = "middle";
        if (bid > 0) {
          ctx.fillStyle = "rgba(255,200,200,0.95)";
          ctx.textAlign = "right";
          ctx.fillText(bid >= 100 ? bid.toFixed(0) : bid.toFixed(1), xCenter - 2, y);
        }
        if (ask > 0) {
          ctx.fillStyle = "rgba(200,255,230,0.95)";
          ctx.textAlign = "left";
          ctx.fillText(ask >= 100 ? ask.toFixed(0) : ask.toFixed(1), xCenter + 2, y);
        }
      }
    }

    // Cell separator (vertical line at xCenter)
    ctx.strokeStyle = "rgba(60,60,60,0.6)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (yHigh !== null && yLow !== null) {
      ctx.moveTo(xCenter, yHigh);
      ctx.lineTo(xCenter, yLow);
      ctx.stroke();
    }

    // Bar footer: signed delta below low
    if (yLow !== null && barPx >= 24) {
      const d = bar.delta || 0;
      ctx.font = `${Math.min(10, Math.max(8, barPx * 0.18))}px JetBrains Mono, monospace`;
      ctx.fillStyle = d >= 0 ? COLORS.up : COLORS.down;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const label = (d >= 0 ? "+" : "") + (Math.abs(d) >= 100 ? d.toFixed(0) : d.toFixed(1));
      ctx.fillText(label, xCenter, yLow + 2);
    }
  }
  ctx.fillStyle = drawnCells > 0 ? "rgba(255,215,0,0.6)" : "#ff0";
  ctx.font = "11px monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.fillText(`FP: bars_drawn=${drawnBars}/${bars.length} cells=${drawnCells} barPx=${barPx.toFixed(0)}`, 12, 8);
  return drawnCells > 0;
}

function drawFootprintWithRetry(canvas, chart, candleSeries, bars, attempt = 0) {
  const ok = drawFootprint(canvas, chart, candleSeries, bars);
  if (!ok && attempt < 8) {
    requestAnimationFrame(() => drawFootprintWithRetry(canvas, chart, candleSeries, bars, attempt + 1));
  }
}

// ─────────────────────────────────────────────────────────────────────────────

export default function CandleChart({ bars: rawBars, dailyVp, priorVp, detections, positions, nakedPocs, historicalSweeps, swingPoints, coupTrades = [], symbol, chartMode, indicators }) {
  // lightweight-charts setData asserts strictly-asc UNIQUE timestamps. Long
  // windows (5D) can emit duplicate/out-of-order ts → crash. Sort asc + dedupe
  // by ts (keep last) once, feed everywhere downstream.
  const bars = useMemo(() => {
    const arr = (rawBars || []).slice().sort((a, b) => a.ts - b.ts);
    const out = [];
    for (const b of arr) {
      if (out.length && out[out.length - 1].ts === b.ts) out[out.length - 1] = b;
      else out.push(b);
    }
    return out;
  }, [rawBars]);
  const containerRef = useRef(null);
  const vpCanvasRef  = useRef(null);
  const fpCanvasRef  = useRef(null);
  const bbCanvasRef  = useRef(null);
  const [showBubbles, setShowBubbles] = useState(() => {
    try { return localStorage.getItem("fb_show_bubbles") !== "0"; } catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem("fb_show_bubbles", showBubbles ? "1" : "0"); } catch {}
  }, [showBubbles]);
  const bubbleHitsRef = useRef([]);
  const [hoverBubble, setHoverBubble] = useState(null);  // {bt, x, y}
  const bigTradesRef = useRef([]);
  const showBubblesRef = useRef(showBubbles);
  useEffect(() => { bigTradesRef.current = detections?.big_trades || []; }, [detections]);
  useEffect(() => { showBubblesRef.current = showBubbles; }, [showBubbles]);
  const chartRef     = useRef(null);
  const candleRef    = useRef(null);
  const deltaRef     = useRef(null);
  const cvdRef       = useRef(null);
  const linesRef       = useRef([]);
  const pivotSeriesRef = useRef([]);
  const markersRef   = useRef([]);
  const [hoverMarker, setHoverMarker] = useState(null);
  const barsRef      = useRef(bars);
  const dailyVpRef   = useRef(dailyVp);
  const prevSymbol   = useRef(symbol);
  barsRef.current    = bars;
  dailyVpRef.current = dailyVp;

  // Init chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: COLORS.bg },
        textColor:  COLORS.text,
        fontFamily: "JetBrains Mono, Fira Code, monospace",
        fontSize:   11,
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2a2a" },
      localization: {
        timeFormatter: (ts) => `${istFormatter(ts)} IST`,
      },
      timeScale: {
        borderColor:       "#2a2a2a",
        timeVisible:       true,
        secondsVisible:    false,
        tickMarkFormatter: istFormatter,
      },
      handleScale: { axisPressedMouseMove: true },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor:         COLORS.up,
      downColor:       COLORS.down,
      borderUpColor:   COLORS.up,
      borderDownColor: COLORS.down,
      wickUpColor:     COLORS.up,
      wickDownColor:   COLORS.down,
    });

    const deltaSeries = chart.addHistogramSeries({
      priceFormat:  { type: "volume" },
      priceScaleId: "delta",
      base:         0,
    });
    chart.priceScale("delta").applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
      borderColor: "#2a2a2a",
    });

    const cvdSeries = chart.addLineSeries({
      color:        "#42a5f5",
      lineWidth:    1,
      priceScaleId: "cvd",
    });
    chart.priceScale("cvd").applyOptions({
      scaleMargins: { top: 0.75, bottom: 0 },
      visible:      false,
    });

    chartRef.current  = chart;
    candleRef.current = candleSeries;
    deltaRef.current  = deltaSeries;
    cvdRef.current    = cvdSeries;

    const redrawAll = () => {
      drawVP(vpCanvasRef.current, chart, candleSeries, dailyVpRef.current, barsRef.current);
      drawFootprint(fpCanvasRef.current, chart, candleSeries, barsRef.current);
      drawBubbles(bbCanvasRef.current, chart, candleSeries, bigTradesRef.current, showBubblesRef.current, barsRef.current, bubbleHitsRef.current);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(redrawAll);
    chart.subscribeCrosshairMove(redrawAll);
    chart.subscribeCrosshairMove(param => {
      if (!param.time || !param.point) { setHoverMarker(null); return; }
      const hit = markersRef.current.find(m => m.time === param.time);
      setHoverMarker(hit ? { ...hit, x: param.point.x, y: param.point.y } : null);
    });

    // Catch all drags (x-pan, y-axis scale, x-axis scale). subscribeCrosshairMove
    // only fires when cursor is over plot; axis-drag may not move crosshair.
    // mousemove on container covers every interaction.
    const onMouseMoveAny = (e) => {
      redrawAll();
      // Hit-test bubble hover. e coords relative to container; canvas is full overlay
      // so same rect → use container's getBoundingClientRect.
      if (!bbCanvasRef.current) return;
      const rect = bbCanvasRef.current.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      let hit = null;
      for (const h of bubbleHitsRef.current) {
        if (Math.hypot(mx - h.x, my - h.y) <= h.r + 2) { hit = h; break; }
      }
      setHoverBubble(prev => {
        if (!hit && !prev) return prev;
        if (hit && prev && prev.bt === hit.bt) return { ...prev, x: mx, y: my };
        return hit ? { bt: hit.bt, x: mx, y: my } : null;
      });
    };
    const onMouseLeave = () => setHoverBubble(null);
    const onMouseWheel = () => redrawAll();
    if (containerRef.current) {
      containerRef.current.addEventListener("mousemove", onMouseMoveAny);
      containerRef.current.addEventListener("mouseleave", onMouseLeave);
      containerRef.current.addEventListener("wheel",     onMouseWheel, { passive: true });
    }

    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      const { clientWidth: w, clientHeight: h } = containerRef.current;
      chart.applyOptions({ width: w, height: h });
      for (const c of [vpCanvasRef.current, fpCanvasRef.current, bbCanvasRef.current]) {
        if (c) { c.width = w; c.height = h; }
      }
      redrawAll();
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(redrawAll);
      if (containerRef.current) {
        containerRef.current.removeEventListener("mousemove", onMouseMoveAny);
        containerRef.current.removeEventListener("mouseleave", onMouseLeave);
        containerRef.current.removeEventListener("wheel",     onMouseWheel);
      }
      chart.remove();
    };
  }, []);

  // Update candle + delta + CVD; fit content on symbol change
  useEffect(() => {
    if (!candleRef.current || !bars.length) return;

    candleRef.current.setData(bars.map(b => ({
      time: b.ts, open: b.o, high: b.h, low: b.l, close: b.c,
    })));

    deltaRef.current.setData(bars.map(b => ({
      time: b.ts, value: b.delta,
      color: b.delta >= 0 ? COLORS.up : COLORS.down,
    })));

    let cvdSum = 0;
    cvdRef.current.setData(bars.map(b => {
      cvdSum += b.delta || 0;
      return { time: b.ts, value: cvdSum };
    }));

    if (prevSymbol.current !== symbol) {
      prevSymbol.current = symbol;
      chartRef.current?.timeScale().fitContent();
    }

    drawVP(vpCanvasRef.current, chartRef.current, candleRef.current, dailyVp, bars);
    // Defer bubble draw a frame so chart's internal coordinate mapping is
    // up-to-date with the just-applied setData call.
    requestAnimationFrame(() => {
      drawBubbles(bbCanvasRef.current, chartRef.current, candleRef.current,
                  detections?.big_trades || [], showBubbles, bars, bubbleHitsRef.current);
    });

    if (chartMode === "footprint") {
      drawFootprintWithRetry(fpCanvasRef.current, chartRef.current, candleRef.current, bars);
    }
  }, [bars, symbol, detections, showBubbles]);

  // On mode flip into OHLC, force a bubble redraw — chart may have stale
  // coordinate mapping while hidden (visibility:hidden); a frame after
  // becoming visible re-resolves it.
  useEffect(() => {
    if (chartMode !== "ohlc") return;
    requestAnimationFrame(() => {
      drawBubbles(bbCanvasRef.current, chartRef.current, candleRef.current,
                  bigTradesRef.current, showBubblesRef.current, barsRef.current, bubbleHitsRef.current);
    });
  }, [chartMode]);

  // Dim/restore candlestick and redraw footprint on mode change.
  // Keep series visible so the price scale stays anchored — priceToCoordinate
  // would return null if the only series with data is hidden.
  useEffect(() => {
    if (!candleRef.current || !chartRef.current) return;

    // FP mode is rendered by FootprintPane (separate component in App.jsx);
    // CandleChart always renders OHLC. Skip mode-driven dim/barSpacing
    // overrides that were resetting user zoom on every bars update.
  }, [chartMode, bars]);

  // Redraw VP when dailyVp changes
  useEffect(() => {
    drawVP(vpCanvasRef.current, chartRef.current, candleRef.current, dailyVp, bars);
  }, [dailyVp]);

  // VP price lines + detection markers + position lines
  useEffect(() => {
    if (!candleRef.current) return;
    const cs = candleRef.current;

    linesRef.current.forEach(l => { try { cs.removePriceLine(l); } catch {} });
    linesRef.current = [];
    pivotSeriesRef.current.forEach(s => { try { chartRef.current?.removeSeries(s); } catch {} });
    pivotSeriesRef.current = [];

    const addLine = (price, color, title, style = LineStyle.Solid) => {
      if (!price) return;
      linesRef.current.push(cs.createPriceLine({
        price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title,
      }));
    };

    // ── Essential: always on ─────────────────────────────────────────────────
    if (dailyVp) {
      addLine(dailyVp.poc, COLORS.poc, "POC");
      addLine(dailyVp.vah, COLORS.vah, "VAH", LineStyle.Dashed);
      addLine(dailyVp.val, COLORS.val, "VAL", LineStyle.Dashed);
    }
    positions.forEach(pos => {
      const lc = pos.side === "long" ? COLORS.legFilled : COLORS.down;
      (pos.leg_prices || []).forEach((price, i) => {
        if (price != null) addLine(price, lc, `L${i + 1}`, LineStyle.Dashed);
      });
      if (pos.take_profit) addLine(pos.take_profit, COLORS.tp, "TP");
      if (pos.stop_loss)   addLine(pos.stop_loss,   COLORS.sl, "SL", LineStyle.Dashed);
    });

    // ── Toggleable ────────────────────────────────────────────────────────────
    if (indicators?.nakedPocs !== false) {
      (nakedPocs || []).forEach(n => {
        const alpha = Math.max(0.25, 0.75 - n.age_sessions * 0.1);
        addLine(n.price, `rgba(255,215,0,${alpha.toFixed(2)})`,
                `nPOC-${n.session[0]}${n.age_sessions}`, LineStyle.Dotted);
      });
    }
    if (indicators?.priorPoc !== false && priorVp?.poc) {
      addLine(priorVp.poc, "rgba(255,215,0,0.35)", "pPOC", LineStyle.Dotted);
    }
    if (indicators?.priorVa !== false && priorVp) {
      addLine(priorVp.vah, "rgba(0,188,212,0.2)",  "pVAH", LineStyle.Dotted);
      addLine(priorVp.val, "rgba(224,64,251,0.2)", "pVAL", LineStyle.Dotted);
    }
    if (indicators?.liveSweep !== false && detections?.sweep?.type && detections.sweep.type !== "none") {
      const sw = detections.sweep;
      addLine(sw.wick_extreme,
        sw.type === "sweep_high" ? "rgba(239,83,80,0.4)" : "rgba(38,166,154,0.4)",
        "", LineStyle.Dotted);
    }
    if (indicators?.fvgLines !== false) {
      (detections?.fvgs || []).forEach(fvg => {
        const c = fvg.side === "bull" ? "rgba(38,166,154,0.5)" : "rgba(239,83,80,0.5)";
        addLine(fvg.high, c, fvg.side === "bull" ? "FVG↑" : "FVG↓", LineStyle.Dotted);
        addLine(fvg.low,  c, "", LineStyle.Dotted);
      });
    }

    const markers = [];

    // Sweep arrows — REV + LG only
    if (indicators?.sweepArrows !== false) {
      (historicalSweeps || []).forEach(sw => {
        const cls = sw.classification || "";
        if (cls !== "reversal" && cls !== "liquidity_grab") return;
        const isHi = sw.type === "sweep_high";
        const color = cls === "liquidity_grab"
          ? (isHi ? "#ff9800" : "#ffb74d")
          : (isHi ? COLORS.down : COLORS.up);
        markers.push({
          time: sw.ts, position: isHi ? "aboveBar" : "belowBar",
          color, shape: isHi ? "arrowDown" : "arrowUp",
          text: cls === "liquidity_grab" ? "LG" : "REV",
          size: sw.confidence >= 0.7 ? 2 : 1,
        });
      });
    }

    // SR/FS sweeps — small dimmed markers with hover text
    if (indicators?.srFsSweeps !== false) {
      (historicalSweeps || []).forEach(sw => {
        const cls = sw.classification || "";
        if (cls !== "stop_run" && cls !== "failed_sweep") return;
        const isHi = sw.type === "sweep_high";
        markers.push({
          time: sw.ts, position: isHi ? "aboveBar" : "belowBar",
          color: cls === "stop_run" ? "rgba(120,120,120,0.6)" : "rgba(156,39,176,0.6)",
          shape: isHi ? "arrowDown" : "arrowUp",
          text: cls === "stop_run" ? "SR" : "FS",
          size: 1,
        });
      });
    }

    // Absorption circles on last bar
    if (indicators?.absorptions !== false && bars.length > 0) {
      const lastTs = bars[bars.length - 1].ts;
      (detections?.absorptions || []).forEach(a => {
        markers.push({
          time: lastTs,
          position: a.side === "buy" ? "belowBar" : "aboveBar",
          color:    a.side === "buy" ? COLORS.up : COLORS.down,
          shape:    "circle",
          text:     `ABS ${a.price}`,
          size:     1,
        });
      });
    }

    // Bounded pivot lines — extend only until level is taken out
    const findTakeout = (sp) => {
      const isHi = sp.side === "high";
      for (const b of bars) {
        if (b.ts <= sp.ts) continue;
        if (isHi ? b.high > sp.price : b.low < sp.price) return b.ts;
      }
      return null;
    };
    const addBoundedLine = (sp, color, lineWidth = 1) => {
      if (!chartRef.current) return;
      const toTs = findTakeout(sp);
      const endTs = toTs ?? (bars.length ? bars[bars.length - 1].ts : null);
      if (!endTs || endTs <= sp.ts) return;
      const s = chartRef.current.addLineSeries({
        color, lineWidth, lineStyle: LineStyle.Dashed,
        crosshairMarkerVisible: false, priceLineVisible: false,
        lastValueVisible: false, autoscaleInfoProvider: () => null,
      });
      s.setData([{ time: sp.ts, value: sp.price }, { time: endTs, value: sp.price }]);
      pivotSeriesRef.current.push(s);
      return toTs; // null = still active
    };

    // EQH/EQL: bounded dashed line + arrow marker; active levels also get axis label via priceLine
    if (indicators?.eqLevels !== false) {
      (swingPoints || []).filter(sp => sp.is_equal).forEach(sp => {
        const isHi = sp.side === "high";
        const takenOut = addBoundedLine(sp, "rgba(255,152,0,0.65)", 1);
        if (takenOut === null) {
          // Still active — add axis label via priceLine
          addLine(sp.price, "rgba(255,152,0,0.0)", isHi ? "EQH" : "EQL", LineStyle.Dashed);
        }
        markers.push({
          time: sp.ts, position: isHi ? "aboveBar" : "belowBar",
          color: "rgba(255,152,0,0.9)", shape: isHi ? "arrowDown" : "arrowUp",
          text: `${isHi ? "EQH" : "EQL"} ${sp.price}`, size: 2,
        });
      });
    }

    // Non-EQ pivot bounded dashed lines only (no arrows, no text)
    if (indicators?.pivotLines !== false) {
      (swingPoints || []).filter(sp => !sp.is_equal).forEach(sp => {
        const isHi = sp.side === "high";
        addBoundedLine(sp, isHi ? "rgba(239,83,80,0.20)" : "rgba(38,166,154,0.20)");
      });
    }

    // ── Coup strategy trades (backtest + live) ───────────────────────────────
    // entry/exit arrows + entry/SL/TP segments spanning entry_ts→exit_ts only
    // (time-bounded line series, like pivots — not full-width price lines).
    const lastBarTs = bars.length ? bars[bars.length - 1].ts : null;
    const seg = (price, color, fromTs, toTs, style, width = 1) => {
      if (price == null || !fromTs || !chartRef.current) return;
      const end = toTs || lastBarTs;
      if (!end || end < fromTs) return;
      const s = chartRef.current.addLineSeries({
        color, lineWidth: width, lineStyle: style,
        crosshairMarkerVisible: false, priceLineVisible: false,
        lastValueVisible: false, autoscaleInfoProvider: () => null,
      });
      s.setData([{ time: fromTs, value: price }, { time: end, value: price }]);
      pivotSeriesRef.current.push(s);
    };
    (coupTrades || []).forEach(t => {
      const isLong = t.side === "long";
      const win = (t.r ?? 0) > 0;
      const col = win ? COLORS.up : COLORS.down;
      markers.push({
        time: t.entry_ts, position: isLong ? "belowBar" : "aboveBar",
        color: col, shape: isLong ? "arrowUp" : "arrowDown",
        text: `C${isLong ? "↑" : "↓"} ${t.entry_mode || ""}`.trim(), size: 2,
      });
      if (t.exit_ts) {
        const rTxt = t.r == null ? "" : `${t.r >= 0 ? "+" : ""}${t.r.toFixed(2)}R`;
        markers.push({
          time: t.exit_ts, position: isLong ? "aboveBar" : "belowBar",
          color: col, shape: "circle", text: `${t.reason || "exit"} ${rTxt}`.trim(), size: 1,
        });
      }
      seg(t.entry, col, t.entry_ts, t.exit_ts, LineStyle.Solid, 2);
      seg(t.sl, COLORS.sl, t.entry_ts, t.exit_ts, LineStyle.Dashed);
      seg(t.tp, COLORS.tp, t.entry_ts, t.exit_ts, LineStyle.Dashed);
    });

    markers.sort((a, b) => a.time - b.time);
    if (markers.length) try { cs.setMarkers(markers); } catch {}
    markersRef.current = markers;

  }, [dailyVp, priorVp, detections, positions, nakedPocs, historicalSweeps, swingPoints, coupTrades, bars, indicators]);

  function resetView() {
    if (chartRef.current) chartRef.current.timeScale().fitContent();
  }

  const canvasStyle = {
    position: "absolute", top: 0, left: 0,
    width: "100%", height: "100%",
    pointerEvents: "none",
  };

  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <div ref={containerRef} style={{ width: "100%", height: "100%" }} />
      {/* VP overlay — always shown */}
      <canvas ref={vpCanvasRef} style={{ ...canvasStyle, zIndex: 5 }} />
      {/* Bubble overlay — always rendered; visibility gated inside drawBubbles */}
      <canvas ref={bbCanvasRef} style={{ ...canvasStyle, zIndex: 6 }} />
      {/* Footprint overlay — FP mode only */}
      <canvas
        ref={fpCanvasRef}
        style={{ ...canvasStyle, display: chartMode === "footprint" ? "block" : "none" }}
      />
      {/* Bubble hover tooltip */}
      {hoverBubble && (() => {
        const bt = hoverBubble.bt;
        const r = (n, dp = 2) => Number.isFinite(n) ? n.toFixed(dp) : "—";
        const isBuy = bt.aggressor === "buy";
        const color = isBuy ? COLORS.up : COLORS.down;
        const W = 230, H = 100;
        const wrapRect = containerRef.current?.getBoundingClientRect() || { width: 800, height: 400 };
        let tx = hoverBubble.x + 12;
        let ty = hoverBubble.y + 12;
        if (tx + W > wrapRect.width)  tx = hoverBubble.x - W - 12;
        if (ty + H > wrapRect.height) ty = hoverBubble.y - H - 12;
        const tIst = bt.ts ? (() => {
          const d = new Date((bt.ts + 19800) * 1000);
          return `${String(d.getUTCHours()).padStart(2,"0")}:${String(d.getUTCMinutes()).padStart(2,"0")} IST`;
        })() : "";
        return (
          <div style={{
            position: "absolute", left: tx, top: ty,
            background: "rgba(13,13,13,0.92)", border: "1px solid #2a2a2a",
            borderRadius: 3, padding: "6px 8px", pointerEvents: "none",
            zIndex: 20, minWidth: W, fontFamily: "JetBrains Mono, monospace",
            fontSize: 11, color: "#bbb", lineHeight: 1.5,
          }}>
            <div style={{ color, fontWeight: 700 }}>
              BIG {isBuy ? "BUY" : "SELL"}{bt.count > 1 ? ` ×${bt.count}` : ""}
            </div>
            <div><span style={{ color: "#888" }}>price </span><span>{r(bt.price)}</span>
              <span style={{ color: "#888" }}>  vol </span><span>{r(bt.volume, 2)}</span></div>
            <div><span style={{ color: "#888" }}>outcome </span><span>{bt.outcome || "—"}</span></div>
            {tIst && <div><span style={{ color: "#888" }}>at </span><span>{tIst}</span></div>}
          </div>
        );
      })()}
      {/* Marker hover tooltip */}
      {hoverMarker && (() => {
        const m = hoverMarker;
        const W = 240, H = 90;
        const wrapRect = containerRef.current?.getBoundingClientRect() || { width: 800, height: 400 };
        let tx = m.x + 12, ty = m.y + 12;
        if (tx + W > wrapRect.width)  tx = m.x - W - 12;
        if (ty + H > wrapRect.height) ty = m.y - H - 12;
        const isHi = m.position === "aboveBar";
        const txt = m.text || "";
        let header = txt, desc = "";
        if (txt === "REV")          { header = `REVERSAL ${isHi ? "HIGH" : "LOW"}`; desc = "Delta-confirmed sweep with strong rejection bar — highest-confidence reversal signal"; }
        else if (txt === "LG")      { header = `LIQUIDITY GRAB ${isHi ? "HIGH" : "LOW"}`; desc = "Institutional sweep of EQH/EQL or named level — delta confirmed, reversal likely"; }
        else if (txt === "SR")      { header = `STOP RUN ${isHi ? "HIGH" : "LOW"}`; desc = "Sweep without delta confirmation — stops taken, direction unclear"; }
        else if (txt === "FS")      { header = `FAILED SWEEP ${isHi ? "HIGH" : "LOW"}`; desc = "Price accepted beyond level — potential breakout, not a reversal"; }
        else if (txt.startsWith("EQH")) { header = "EQUAL HIGH"; desc = "Retail stop cluster — two+ pivots within 0.15%. Prime institutional sweep target before reversal"; }
        else if (txt.startsWith("EQL")) { header = "EQUAL LOW";  desc = "Retail stop cluster — two+ pivots within 0.15%. Prime institutional sweep target before reversal"; }
        else if (txt.startsWith("ABS")) { header = `ABSORPTION ${isHi ? "SELL" : "BUY"}`; desc = "Large passive volume at extreme with minimal movement — potential reversal signal"; }
        return (
          <div style={{
            position: "absolute", left: tx, top: ty,
            background: "rgba(13,13,13,0.92)", border: "1px solid #2a2a2a",
            borderRadius: 3, padding: "6px 8px", pointerEvents: "none",
            zIndex: 20, minWidth: W, fontFamily: "JetBrains Mono, monospace",
            fontSize: 11, color: "#bbb", lineHeight: 1.5,
          }}>
            <div style={{ color: m.color, fontWeight: 700 }}>{header}</div>
            {desc && <div style={{ color: "#888", marginTop: 2 }}>{desc}</div>}
          </div>
        );
      })()}
      {/* TV-style indicator list — top-left */}
      <div style={{
        position: "absolute", top: 6, left: 6, zIndex: 10,
        display: "flex", flexDirection: "column", gap: 4,
        fontFamily: "monospace", fontSize: 11,
      }}>
        <button
          onClick={() => setShowBubbles(b => !b)}
          title="Toggle big-trade bubbles"
          style={{
            background: "rgba(26,26,26,0.85)",
            border: `1px solid ${showBubbles ? "#ffd700" : "#2a2a2a"}`,
            color: showBubbles ? "#ffd700" : "#666",
            padding: "2px 7px", cursor: "pointer",
            borderRadius: 3, lineHeight: 1.4, textAlign: "left",
          }}
        >
          {showBubbles ? "● BUBBLES" : "○ bubbles"}
        </button>
      </div>
      <button
        onClick={resetView}
        title="Reset view"
        style={{
          position: "absolute", top: 6, right: 6,
          background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
          color: "#888", fontSize: 11, padding: "2px 7px", cursor: "pointer",
          fontFamily: "monospace", borderRadius: 3, zIndex: 10, lineHeight: 1.4,
        }}
        onMouseEnter={e => e.currentTarget.style.color = "#ccc"}
        onMouseLeave={e => e.currentTarget.style.color = "#888"}
      >
        ⊡
      </button>
    </div>
  );
}
