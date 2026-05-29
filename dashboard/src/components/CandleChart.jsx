import { useEffect, useRef } from "react";
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

export default function CandleChart({ bars, dailyVp, priorVp, detections, positions, symbol, chartMode }) {
  const containerRef = useRef(null);
  const vpCanvasRef  = useRef(null);
  const fpCanvasRef  = useRef(null);
  const chartRef     = useRef(null);
  const candleRef    = useRef(null);
  const deltaRef     = useRef(null);
  const cvdRef       = useRef(null);
  const linesRef     = useRef([]);
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
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(redrawAll);
    chart.subscribeCrosshairMove(redrawAll);

    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      const { clientWidth: w, clientHeight: h } = containerRef.current;
      chart.applyOptions({ width: w, height: h });
      for (const c of [vpCanvasRef.current, fpCanvasRef.current]) {
        if (c) { c.width = w; c.height = h; }
      }
      redrawAll();
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(redrawAll);
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

    if (chartMode === "footprint") {
      drawFootprintWithRetry(fpCanvasRef.current, chartRef.current, candleRef.current, bars);
    }
  }, [bars, symbol]);

  // Dim/restore candlestick and redraw footprint on mode change.
  // Keep series visible so the price scale stays anchored — priceToCoordinate
  // would return null if the only series with data is hidden.
  useEffect(() => {
    if (!candleRef.current || !chartRef.current) return;

    if (chartMode === "footprint") {
      candleRef.current.applyOptions({
        upColor:         "rgba(38,166,154,0.10)",
        downColor:       "rgba(239,83,80,0.10)",
        borderUpColor:   "rgba(38,166,154,0.20)",
        borderDownColor: "rgba(239,83,80,0.20)",
        wickUpColor:     "rgba(38,166,154,0.30)",
        wickDownColor:   "rgba(239,83,80,0.30)",
      });
      chartRef.current.timeScale().applyOptions({ barSpacing: 60, minBarSpacing: 20 });
      const c = fpCanvasRef.current;
      if (c && c.parentElement) {
        c.width  = c.parentElement.clientWidth;
        c.height = c.parentElement.clientHeight;
      }
      requestAnimationFrame(() => {
        drawFootprintWithRetry(fpCanvasRef.current, chartRef.current, candleRef.current, barsRef.current);
      });
    } else {
      candleRef.current.applyOptions({
        upColor:         COLORS.up,
        downColor:       COLORS.down,
        borderUpColor:   COLORS.up,
        borderDownColor: COLORS.down,
        wickUpColor:     COLORS.up,
        wickDownColor:   COLORS.down,
      });
      chartRef.current.timeScale().applyOptions({ barSpacing: 6, minBarSpacing: 0.5 });
      const c = fpCanvasRef.current;
      if (c) c.getContext("2d").clearRect(0, 0, c.width, c.height);
    }
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

    const addLine = (price, color, title, style = LineStyle.Solid) => {
      if (!price) return;
      linesRef.current.push(cs.createPriceLine({
        price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title,
      }));
    };

    if (dailyVp) {
      addLine(dailyVp.poc, COLORS.poc, "POC");
      addLine(dailyVp.vah, COLORS.vah, "VAH", LineStyle.Dashed);
      addLine(dailyVp.val, COLORS.val, "VAL", LineStyle.Dashed);
      if (dailyVp.naked_poc) addLine(dailyVp.naked_poc, COLORS.poc, "nPOC", LineStyle.Dotted);
    }
    if (priorVp) {
      addLine(priorVp.poc, "rgba(255,215,0,0.35)", "pPOC", LineStyle.Dotted);
      addLine(priorVp.vah, "rgba(0,188,212,0.25)",  "pVAH", LineStyle.Dotted);
      addLine(priorVp.val, "rgba(224,64,251,0.25)",  "pVAL", LineStyle.Dotted);
    }
    if (detections?.sweep?.type !== "none" && detections?.sweep) {
      const sw = detections.sweep;
      addLine(sw.wick_extreme, sw.type === "sweep_high" ? COLORS.down : COLORS.up,
              `sw:${sw.level_label}`, LineStyle.Dotted);
    }
    (detections?.fvgs || []).forEach(fvg => {
      const c = fvg.side === "bull" ? "rgba(38,166,154,0.6)" : "rgba(239,83,80,0.6)";
      addLine(fvg.high, c, fvg.side === "bull" ? "FVG↑" : "FVG↓", LineStyle.Dotted);
      addLine(fvg.low,  c, "", LineStyle.Dotted);
    });
    positions.forEach(pos => {
      const lc = pos.side === "long" ? COLORS.legFilled : COLORS.down;
      (pos.leg_prices || []).forEach((price, i) => {
        if (price != null) addLine(price, lc, `L${i + 1}`, LineStyle.Dashed);
      });
      if (pos.take_profit) addLine(pos.take_profit, COLORS.tp, "TP");
      if (pos.stop_loss)   addLine(pos.stop_loss,   COLORS.sl, "SL", LineStyle.Dashed);
    });

    const markers = [];
    if (bars.length > 0) {
      const lastTs = bars[bars.length - 1].ts;
      if (detections?.sweep?.type && detections.sweep.type !== "none") {
        const sw = detections.sweep;
        markers.push({
          time: lastTs,
          position: sw.type === "sweep_high" ? "aboveBar" : "belowBar",
          color:    sw.type === "sweep_high" ? COLORS.down : COLORS.up,
          shape:    sw.type === "sweep_high" ? "arrowDown" : "arrowUp",
          text:     `SW ${sw.level_label}`,
          size:     1,
        });
      }
      (detections?.absorptions || []).forEach(a => {
        markers.push({
          time: lastTs,
          position: a.side === "buy" ? "belowBar" : "aboveBar",
          color:    a.side === "buy" ? COLORS.up : COLORS.down,
          shape:    "circle",
          text:     `ABS ${a.side}`,
          size:     1,
        });
      });
    }
    if (markers.length) try { cs.setMarkers(markers); } catch {}

  }, [dailyVp, priorVp, detections, positions, bars]);

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
      <canvas ref={vpCanvasRef} style={canvasStyle} />
      {/* Footprint overlay — FP mode only */}
      <canvas
        ref={fpCanvasRef}
        style={{ ...canvasStyle, display: chartMode === "footprint" ? "block" : "none" }}
      />
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
