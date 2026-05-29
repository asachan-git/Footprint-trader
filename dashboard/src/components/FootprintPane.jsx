import { useEffect, useRef, useState } from "react";

// Sierra-style dedicated footprint pane.
// Own canvas + own price/time math — no lightweight-charts dependency.
// - Right-aligned: newest bar on the right.
// - Mouse wheel: zoom Y (price scale).
// - Drag: pan Y.
// - Bars width fixed (configurable). Auto-fits to viewport from the right.

const COLORS = {
  bg:    "#0d0d0d",
  text:  "#888",
  textDim: "#555",
  grid:  "#1a1a1a",
  up:    "#26a69a",
  down:  "#ef5350",
  poc:   "rgba(255,215,0,0.35)",
  pocBorder: "rgba(255,215,0,0.95)",
  vah:   "#00bcd4",
  val:   "#e040fb",
  axis:  "#2a2a2a",
};

const BAR_W_DEFAULT = 100;  // default px per bar
const PRICE_AXIS  = 64;
const TIME_AXIS   = 22;
const PAD_TOP     = 6;
const MIN_CELL_PX = 11;     // below this, merge ticks into wider rows
const CANDLE_W    = 10;     // candle body width in center of bar
const RIGHT_PAD_BARS = 2;   // empty bar-widths after the latest bar
const VP_WIDTH_PX = 60;     // right-side daily VP histogram width
const VOL_STRIP_H = 38;     // dedicated volume strip below main plot
const IMBALANCE_RATIO = 3.0;
const STACKED_MIN     = 3;
const IST = 19800;

function inferTickSize(bars) {
  // Smallest positive diff between adjacent ladder prices across all bars.
  let tick = 0;
  for (const b of bars) {
    const prices = new Set();
    (b.bid_ladder || []).forEach(l => prices.add(l.p));
    (b.ask_ladder || []).forEach(l => prices.add(l.p));
    const sorted = [...prices].sort((a, b) => a - b);
    for (let i = 1; i < sorted.length; i++) {
      const d = +(sorted[i] - sorted[i - 1]).toFixed(8);
      if (d > 0 && (tick === 0 || d < tick)) tick = d;
    }
  }
  if (tick === 0 && bars.length) tick = Math.max(bars[0].c * 0.0001, 0.01);
  return tick || 0.01;
}

export default function FootprintPane({
  bars, dailyVp, priorVp, detections, positions, pendingOrders,
  closedPositions, cvd, cvdSignal, zones, latestM2, symbol,
}) {
  const canvasRef = useRef(null);
  const wrapRef   = useRef(null);
  const [view, setView] = useState({
    pxPerTick:  14,
    centerPrice: null,
    barW:       BAR_W_DEFAULT,
    offsetBars: 0,
    autoFit:    true,
    cvdH:       70,
    yLocked:    false,
    cvdPan:     0,              // CVD Y pan offset (px, positive = view up)
    cvdZoom:    1,              // CVD Y zoom factor (>1 = stretch range)
  });
  const dragRef = useRef(null);
  const fitRef  = useRef({ pxPerTick: 14, centerPrice: null });
  const [cellMode, setCellMode] = useState("bidask"); // bidask | delta | total
  const [hoverBarIdx, setHoverBarIdx] = useState(null); // index in `bars` of crosshaired bar
  const [showTrades, setShowTrades] = useState(true);   // toggle trade-history overlay
  const [selectedPosId, setSelectedPosId] = useState(null);
  const markerHitsRef = useRef([]);  // [{x, y, r, posId}] populated each draw
  const cvdPaneRef    = useRef(null);

  // Reset view when instrument changes — refit to its market range
  useEffect(() => {
    setView({
      pxPerTick: 14, centerPrice: null,
      barW: BAR_W_DEFAULT, offsetBars: 0, autoFit: true, yLocked: false, cvdPan: 0, cvdZoom: 1,
    });
  }, [symbol]);

  // Draw
  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap || !bars.length) return;

    const dpr  = window.devicePixelRatio || 1;
    const cssW = wrap.clientWidth;
    const cssH = wrap.clientHeight;
    canvas.width  = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width  = cssW + "px";
    canvas.style.height = cssH + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, cssW, cssH);

    // Plot regions: main canvas spans full width to price axis. VP renders
    // as a semi-transparent overlay anchored to the right edge of the plot.
    const plotW    = cssW - PRICE_AXIS;
    const CVD_PANE_H = (cvd && cvd.length) ? Math.max(30, Math.min(300, view.cvdH || 70)) : 0;
    const fullPlotH  = cssH - TIME_AXIS - PAD_TOP;
    const plotH      = fullPlotH - CVD_PANE_H - VOL_STRIP_H;
    const volTop     = PAD_TOP + plotH + 1;
    const volBot     = volTop + VOL_STRIP_H - 2;
    const cvdTop     = volBot + 2;

    // Slot-based pan model (TV-like). `view.offsetBars` is a float:
    //   0  = latest closed bar at the rightmost slot (anchored)
    //  +N  = N bars back at the rightmost slot (scrolled into the past)
    // Right padding shrinks as user pans into the past so the latest candles
    // stay visible (no padding-induced hiding when scrolling back).
    const barW       = view.barW;
    const effPad     = Math.max(0, RIGHT_PAD_BARS - Math.max(0, view.offsetBars));
    const usableW    = plotW - effPad * barW;
    const fitN       = Math.max(1, Math.floor(usableW / barW));
    const maxOffset  = Math.max(0, bars.length - 1);   // can scroll back to first bar
    maxOffsetRef.current = maxOffset;
    const offsetFloat = Math.max(0, Math.min(view.offsetBars, maxOffset));
    const offsetInt   = Math.floor(offsetFloat);
    const subBarPx    = (offsetFloat - offsetInt) * barW;   // [0, barW)
    // rightBarIdx: the bars[] index that occupies the rightmost slot (slot=fitN-1)
    const rightBarIdx = bars.length - 1 - offsetInt;
    // Build slot→bar mapping; each slot s renders bars[rightBarIdx - (fitN-1-s)]
    // Render fitN+1 slots (extra slot on left for partial slide-in).
    const slots = [];
    for (let s = -1; s < fitN; s++) {
      const barIdx = rightBarIdx - (fitN - 1 - s);
      if (barIdx >= 0 && barIdx < bars.length) {
        slots.push({ slot: s, barIdx, bar: bars[barIdx] });
      }
    }
    const slice = slots.map(x => x.bar);   // bar objects in slot order (for range/lookup)
    sliceMetaRef.current = { rightBarIdx, fitN, barW, subBarPx };

    const tick = inferTickSize(slice);
    if (tick <= 0) return;

    // Price range over visible slice
    const lowest  = Math.min(...slice.map(b => b.l));
    const highest = Math.max(...slice.map(b => b.h));
    const priceSpan = Math.max(tick, highest - lowest);

    // Y auto-fit:
    //  - autoFit (init):     compute pxPerTick + center from slice
    //  - !yLocked  (default): re-fit BOTH each draw — bars always vertically in view
    //  - yLocked:            user locked Y — hold their pxPerTick + centerPrice
    let pxPerTick = view.pxPerTick;
    const sliceMid = (lowest + highest) / 2;
    let center;
    if (view.yLocked) {
      center = view.centerPrice ?? sliceMid;
    } else {
      const ticksInRange = priceSpan / tick;
      pxPerTick = Math.max(0.2, Math.min(40, (plotH * 0.9) / ticksInRange));
      center = sliceMid;
    }
    fitRef.current = { pxPerTick, centerPrice: center };

    // Tick aggregation: merge multiple ticks into one row when zoomed out
    const ticksPerRow = Math.max(1, Math.ceil(MIN_CELL_PX / pxPerTick));
    const rowStep     = tick * ticksPerRow;
    const cellH       = pxPerTick * ticksPerRow;
    const pxPerPrice  = pxPerTick / tick;

    const yOfPrice = (p) => PAD_TOP + plotH / 2 - (p - center) * pxPerPrice;
    const priceOfY = (y) => center - (y - PAD_TOP - plotH / 2) / pxPerPrice;

    // Background grid: every Nth tick line
    const visibleTopPrice = priceOfY(PAD_TOP);
    const visibleBotPrice = priceOfY(PAD_TOP + plotH);
    const gridStep = Math.max(1, Math.round(20 / pxPerTick)) * tick;
    const startP = Math.ceil(visibleBotPrice / gridStep) * gridStep;
    ctx.strokeStyle = COLORS.grid;
    ctx.lineWidth = 1;
    ctx.font = "10px JetBrains Mono, monospace";
    ctx.textBaseline = "middle";
    for (let p = startP; p <= visibleTopPrice; p += gridStep) {
      const y = yOfPrice(p);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(plotW, y);
      ctx.stroke();
      ctx.fillStyle = COLORS.textDim;
      ctx.textAlign = "left";
      ctx.fillText(p.toFixed(tick < 0.1 ? 2 : 1), plotW + 4, y);
    }

    // Helpers — slot-based positioning.
    // xCenterOfBar takes a GLOBAL bars[] index and returns its slot's center x.
    const xCenterOfSlot = (slot) => slot * barW + barW / 2 - subBarPx;
    const slotOfBar     = (globalIdx) => globalIdx - rightBarIdx + (fitN - 1);
    const xCenterOfBar  = (globalIdx) => xCenterOfSlot(slotOfBar(globalIdx));
    const tsToVisibleX = (ts) => {
      if (!slots.length) return 0;
      if (ts <= slots[0].bar.ts) return 0;
      const lastSlot = slots[slots.length - 1];
      if (ts >= lastSlot.bar.ts) return xCenterOfSlot(lastSlot.slot);
      const hit = slots.find(s => s.bar.ts >= ts);
      return hit ? xCenterOfSlot(hit.slot) : plotW;
    };

    // HVN / LVN bands (full plot width, faint)
    const drawZoneBand = (lo, hi, fill) => {
      const yTop = yOfPrice(hi);
      const yBot = yOfPrice(lo);
      if (yBot < PAD_TOP || yTop > PAD_TOP + plotH) return;
      const yA = Math.max(PAD_TOP, Math.min(yTop, yBot));
      const yB = Math.min(PAD_TOP + plotH, Math.max(yTop, yBot));
      if (yB <= yA) return;
      ctx.fillStyle = fill;
      ctx.fillRect(0, yA, plotW, yB - yA);
    };
    (dailyVp?.hvn_zones || []).forEach(z => drawZoneBand(z.low, z.high, "rgba(66,165,245,0.07)"));
    (dailyVp?.lvn_zones || []).forEach(z => drawZoneBand(z.low, z.high, "rgba(255,152,0,0.07)"));

    // FVG zones (rectangle from formed_at_ts → right edge)
    (detections?.fvgs || []).forEach(f => {
      const yHi = yOfPrice(f.high);
      const yLo = yOfPrice(f.low);
      const yA  = Math.max(PAD_TOP, Math.min(yHi, yLo));
      const yB  = Math.min(PAD_TOP + plotH, Math.max(yHi, yLo));
      if (yB <= yA) return;
      const xA = tsToVisibleX(f.formed_at_ts);
      const xB = plotW;
      const fill   = f.side === "bull" ? "rgba(38,166,154,0.10)" : "rgba(239,83,80,0.10)";
      const stroke = f.side === "bull" ? "rgba(38,166,154,0.55)" : "rgba(239,83,80,0.55)";
      ctx.fillStyle = fill;
      ctx.fillRect(xA, yA, xB - xA, yB - yA);
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1;
      ctx.strokeRect(xA, yA, xB - xA, yB - yA);
      ctx.fillStyle = stroke;
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText(f.side === "bull" ? "FVG↑" : "FVG↓", xA + 3, yA + 2);
    });

    // VP level lines (POC, VAH, VAL, prior POC)
    const drawLevel = (price, color, label, dashed = false) => {
      if (price == null) return;
      const y = yOfPrice(price);
      if (y < PAD_TOP || y > PAD_TOP + plotH) return;
      ctx.save();
      if (dashed) ctx.setLineDash([4, 3]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(plotW, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = color;
      ctx.textAlign = "left";
      ctx.fillText(label, 4, y - 7);
    };
    if (dailyVp) {
      drawLevel(dailyVp.poc, COLORS.pocBorder, "POC");
      drawLevel(dailyVp.vah, COLORS.vah, "VAH", true);
      drawLevel(dailyVp.val, COLORS.val, "VAL", true);
      if (dailyVp.naked_poc) drawLevel(dailyVp.naked_poc, COLORS.pocBorder, "nPOC", true);
    }
    if (priorVp) {
      drawLevel(priorVp.poc, "rgba(255,215,0,0.35)", "pPOC", true);
    }

    // Strong-zone ticks on plot right edge (HVN/POC/Swings/FVG/Sweep/CVD)
    const zoneTypeChar = (src) => {
      if (!src) return "?";
      if (src.startsWith("hvn"))     return "H";
      if (src.startsWith("vp_poc"))  return "P";
      if (src.startsWith("vp_vah") || src.startsWith("vp_val")) return "P";
      if (src.includes("swing") || src.includes("session") || src.includes("day")) return "S";
      if (src.startsWith("fvg"))     return "F";
      if (src.startsWith("sweep"))   return "W";
      if (src.startsWith("cvd"))     return "C";
      if (src.startsWith("lvn"))     return "L";
      return "?";
    };
    const drawZoneTick = (z, color) => {
      const y = yOfPrice(z.price);
      if (y < PAD_TOP || y > PAD_TOP + plotH) return;
      ctx.fillStyle = color;
      ctx.fillRect(plotW - 5, y - 1, 5, 2);
      ctx.fillStyle = color;
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      ctx.fillText(zoneTypeChar(z.source), plotW - 7, y);
    };
    (zones?.long  || []).forEach(z => drawZoneTick(z, "rgba(38,166,154,0.85)"));
    (zones?.short || []).forEach(z => drawZoneTick(z, "rgba(239,83,80,0.85)"));

    // Pending limit orders (dashed horizontal across full plot)
    (pendingOrders || []).forEach(po => {
      const y = yOfPrice(po.limit_price);
      if (y < PAD_TOP || y > PAD_TOP + plotH) return;
      const color = po.side === "long" ? COLORS.up : COLORS.down;
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(plotW, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = color;
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(`P L${po.leg_idx}`, 4, y - 2);
    });

    // Trade history overlays: closed cycles (MT5-style entry→exit) + open
    // positions. Gated on showTrades toggle.
    markerHitsRef.current = [];

    if (showTrades) (closedPositions || []).forEach(pos => {
      if (!pos.opened_ts || !pos.closed_ts) return;
      const xEntry = tsToVisibleX(pos.opened_ts);
      const xExit  = tsToVisibleX(pos.closed_ts);
      const yEntry = yOfPrice(pos.avg_entry);
      const yExit  = yOfPrice(pos.exit_price);
      if (yEntry < PAD_TOP || yEntry > PAD_TOP + plotH) return;
      if (yExit  < PAD_TOP || yExit  > PAD_TOP + plotH) return;
      const profit = (pos.realized_r || 0) >= 0;
      const lineColor = profit ? "rgba(38,166,154,0.85)" : "rgba(239,83,80,0.85)";
      // Connecting line entry → exit
      ctx.save();
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(xEntry, yEntry);
      ctx.lineTo(xExit,  yExit);
      ctx.stroke();
      ctx.restore();
      // Entry marker (small filled square)
      const entryColor = pos.side === "long" ? COLORS.up : COLORS.down;
      ctx.fillStyle = entryColor;
      ctx.fillRect(xEntry - 4, yEntry - 4, 8, 8);
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.lineWidth = 1;
      ctx.strokeRect(xEntry - 4, yEntry - 4, 8, 8);
      // Exit marker (X glyph)
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(xExit - 5, yExit - 5); ctx.lineTo(xExit + 5, yExit + 5);
      ctx.moveTo(xExit + 5, yExit - 5); ctx.lineTo(xExit - 5, yExit + 5);
      ctx.stroke();
      // R label near exit
      ctx.fillStyle = lineColor;
      ctx.font = "bold 10px JetBrains Mono, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const rLbl = `${profit ? "+" : ""}${(pos.realized_r || 0).toFixed(2)}R`;
      ctx.fillText(rLbl, xExit + 8, yExit);
      markerHitsRef.current.push({ x: xEntry, y: yEntry, r: 6, posId: pos.position_id });
    });

    if (showTrades) (positions || []).forEach(pos => {
      const color = pos.side === "long" ? COLORS.up : COLORS.down;
      const xStart = pos.opened_ts ? tsToVisibleX(pos.opened_ts) : 0;
      const drawHLine = (price, style, lbl) => {
        if (price == null) return;
        const y = yOfPrice(price);
        if (y < PAD_TOP || y > PAD_TOP + plotH) return;
        ctx.save();
        if (style === "dashed") ctx.setLineDash([4, 3]);
        ctx.strokeStyle = style === "dashed" ? color
                        : style === "tp"     ? "rgba(255,215,0,0.9)"
                        : "rgba(239,83,80,0.9)";
        ctx.lineWidth = style === "tp" || style === "sl" ? 1.5 : 1;
        ctx.beginPath();
        ctx.moveTo(xStart, y);
        ctx.lineTo(plotW, y);
        ctx.stroke();
        ctx.restore();
        ctx.fillStyle = ctx.strokeStyle;
        ctx.font = "10px JetBrains Mono, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(lbl, xStart + 4, y - 2);
      };
      (pos.leg_prices || []).forEach((price, i) => {
        if (price != null) drawHLine(price, "dashed", `L${i + 1}`);
      });
      drawHLine(pos.take_profit, "tp", "TP");
      drawHLine(pos.stop_loss,   "sl", "SL");

      // Entry-marker diamond at (xStart, avg_entry). Hit-testable.
      const yEntry = yOfPrice(pos.avg_entry);
      if (yEntry >= PAD_TOP && yEntry <= PAD_TOP + plotH && xStart >= 0 && xStart <= plotW) {
        const isSelected = selectedPosId === pos.position_id;
        const sz = isSelected ? 8 : 6;
        ctx.fillStyle   = color;
        ctx.strokeStyle = "rgba(255,255,255,0.9)";
        ctx.lineWidth   = isSelected ? 2 : 1;
        ctx.beginPath();
        ctx.moveTo(xStart, yEntry - sz);
        ctx.lineTo(xStart + sz, yEntry);
        ctx.lineTo(xStart, yEntry + sz);
        ctx.lineTo(xStart - sz, yEntry);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        // Side glyph (▲/▼)
        ctx.fillStyle = "#fff";
        ctx.font = "bold 9px JetBrains Mono, monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(pos.side === "long" ? "▲" : "▼", xStart, yEntry);
        markerHitsRef.current.push({ x: xStart, y: yEntry, r: sz + 2, posId: pos.position_id });
      }
    });

    // Auto-fallback: when bars are too narrow OR cells too thin, footprint
    // becomes illegible. Render plain candlesticks instead.
    const fallbackToCandle = barW < 30 || cellH < 4;

    // Clip all bar/cell/marker rendering to the main pane rect so bars/wicks
    // don't bleed into the CVD pane below.
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, PAD_TOP, plotW, plotH);
    ctx.clip();

    // Bars — render per slot (slot model). Skip bars outside plot bounds.
    slots.forEach(({ slot, bar }) => {
      const xLeft   = slot * barW - subBarPx;
      const xCenter = xLeft + barW / 2;
      if (xLeft + barW < 0 || xLeft > plotW) return;
      if (fallbackToCandle) {
        // Plain candlestick (wick + body), no cells.
        const yO = yOfPrice(bar.o);
        const yC = yOfPrice(bar.c);
        const yH = yOfPrice(bar.h);
        const yL = yOfPrice(bar.l);
        const isUp = bar.c >= bar.o;
        const color = isUp ? COLORS.up : COLORS.down;
        const bodyW = Math.max(1, barW * 0.6);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(xCenter, yH);
        ctx.lineTo(xCenter, yL);
        ctx.stroke();
        ctx.fillStyle = color;
        const bodyTop = Math.min(yO, yC);
        ctx.fillRect(xCenter - bodyW / 2, bodyTop, bodyW, Math.max(1, Math.abs(yO - yC)));
        return;
      }
      const halfBar = barW / 2 - 2;
      const cellSide = halfBar - CANDLE_W / 2 - 1;       // width of bid/ask cell column

      // Bucket ladders by aggregated row key (multiple ticks per row when zoomed out)
      const bidRow = {};
      const askRow = {};
      const tickKey = p => Math.round(p / tick);
      const rowKey  = p => Math.floor(tickKey(p) / ticksPerRow);
      (bar.bid_ladder || []).forEach(l => {
        if (l.v > 0) bidRow[rowKey(l.p)] = (bidRow[rowKey(l.p)] || 0) + l.v;
      });
      (bar.ask_ladder || []).forEach(l => {
        if (l.v > 0) askRow[rowKey(l.p)] = (askRow[rowKey(l.p)] || 0) + l.v;
      });
      const rows = [...new Set([...Object.keys(bidRow), ...Object.keys(askRow)])]
        .map(Number).sort((a, b) => a - b);

      const maxVol = rows.reduce(
        (m, r) => Math.max(m, (bidRow[r] || 0) + (askRow[r] || 0)), 0.001,
      );

      // Diagonal imbalances at row granularity
      const askImb = {};
      const bidImb = {};
      for (const r of rows) {
        const ask     = askRow[r] || 0;
        const bidDiag = bidRow[r - 1] || 0;
        const bid     = bidRow[r] || 0;
        const askDiag = askRow[r + 1] || 0;
        askImb[r] = ask > 0 && ask >= IMBALANCE_RATIO * Math.max(bidDiag, 0.001);
        bidImb[r] = bid > 0 && bid >= IMBALANCE_RATIO * Math.max(askDiag, 0.001);
      }
      const askStack = new Set();
      const bidStack = new Set();
      for (let i = 0; i < rows.length; i++) {
        let j = i;
        while (j < rows.length && askImb[rows[j]]) j++;
        if (j - i >= STACKED_MIN) for (let k = i; k < j; k++) askStack.add(rows[k]);
        if (j > i) i = j - 1;
      }
      for (let i = 0; i < rows.length; i++) {
        let j = i;
        while (j < rows.length && bidImb[rows[j]]) j++;
        if (j - i >= STACKED_MIN) for (let k = i; k < j; k++) bidStack.add(rows[k]);
        if (j > i) i = j - 1;
      }

      const showText = cellH >= 9 && cellSide >= 14;

      // Per-bar max abs delta + max-vol row (for highlight)
      let maxAbsDelta = 0.001;
      let maxVolRow   = null;
      let maxVolSeen  = -1;
      for (const r of rows) {
        const t = (bidRow[r] || 0) + (askRow[r] || 0);
        const d = Math.abs((askRow[r] || 0) - (bidRow[r] || 0));
        if (d > maxAbsDelta) maxAbsDelta = d;
        if (t > maxVolSeen) { maxVolSeen = t; maxVolRow = r; }
      }

      // Footprint cells (skip middle reserved for candle)
      for (const r of rows) {
        const price = r * rowStep + rowStep / 2;       // center of merged row
        const y = yOfPrice(price);
        if (y < PAD_TOP - cellH || y > PAD_TOP + plotH + cellH) continue;

        const bid = bidRow[r] || 0;
        const ask = askRow[r] || 0;
        const isPoc = bar.poc && Math.abs(price - bar.poc) < rowStep * 0.6;

        const bidShare = bid / maxVol;
        const askShare = ask / maxVol;

        const xBidL = xCenter - halfBar;
        const xBidR = xCenter - CANDLE_W / 2;
        const xAskL = xCenter + CANDLE_W / 2;
        const xAskR = xCenter + halfBar;

        // Cell backgrounds — vary by display mode
        if (cellMode === "bidask") {
          if (bid > 0) {
            ctx.fillStyle = `rgba(239,83,80,${0.30 + bidShare * 0.60})`;
            ctx.fillRect(xBidL, y - cellH / 2, cellSide, cellH);
          }
          if (ask > 0) {
            ctx.fillStyle = `rgba(38,166,154,${0.30 + askShare * 0.60})`;
            ctx.fillRect(xAskL, y - cellH / 2, cellSide, cellH);
          }
        } else if (cellMode === "total") {
          // Gray gradient by total share, both columns
          const totalShare = (bid + ask) / maxVol;
          ctx.fillStyle = `rgba(180,180,180,${0.10 + totalShare * 0.55})`;
          ctx.fillRect(xBidL, y - cellH / 2, cellSide, cellH);
          ctx.fillRect(xAskL, y - cellH / 2, cellSide, cellH);
        } else { // delta
          // Left col = total vol gray gradient; right col = signed delta gradient
          const totalShare = (bid + ask) / maxVol;
          ctx.fillStyle = `rgba(180,180,180,${0.10 + totalShare * 0.55})`;
          ctx.fillRect(xBidL, y - cellH / 2, cellSide, cellH);
          const d = ask - bid;
          const dShare = Math.abs(d) / maxAbsDelta;
          ctx.fillStyle = d >= 0
            ? `rgba(38,166,154,${0.20 + dShare * 0.70})`
            : `rgba(239,83,80,${0.20 + dShare * 0.70})`;
          ctx.fillRect(xAskL, y - cellH / 2, cellSide, cellH);
        }

        ctx.strokeStyle = "rgba(0,0,0,0.55)";
        ctx.lineWidth = 1;
        ctx.strokeRect(xBidL, y - cellH / 2, cellSide, cellH);
        ctx.strokeRect(xAskL, y - cellH / 2, cellSide, cellH);

        if (isPoc) {
          ctx.fillStyle = COLORS.poc;
          ctx.fillRect(xBidL, y - cellH / 2, cellSide, cellH);
          ctx.fillRect(xAskL, y - cellH / 2, cellSide, cellH);
          ctx.strokeStyle = COLORS.pocBorder;
          ctx.lineWidth = 1.5;
          ctx.strokeRect(xBidL, y - cellH / 2, cellSide, cellH);
          ctx.strokeRect(xAskL, y - cellH / 2, cellSide, cellH);
        }

        // Mark per-bar max-vol bin (distinct from VP POC) with a left+right gold tick
        if (r === maxVolRow && !isPoc) {
          ctx.fillStyle = "rgba(255,215,0,0.85)";
          ctx.fillRect(xBidL - 2, y - cellH / 2, 2, cellH);
          ctx.fillRect(xAskR,     y - cellH / 2, 2, cellH);
        }

        if (askImb[r]) {
          ctx.fillStyle = askStack.has(r) ? "rgba(38,166,154,0.95)" : "rgba(38,166,154,0.55)";
          ctx.fillRect(xAskR - 3, y - cellH / 2, 3, cellH);
        }
        if (bidImb[r]) {
          ctx.fillStyle = bidStack.has(r) ? "rgba(239,83,80,0.95)" : "rgba(239,83,80,0.55)";
          ctx.fillRect(xBidL, y - cellH / 2, 3, cellH);
        }

        if (showText) {
          ctx.font = `${Math.min(10, cellH - 2)}px JetBrains Mono, monospace`;
          ctx.textBaseline = "middle";
          const fmt = (n) => Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(2);
          if (cellMode === "bidask") {
            if (bid > 0) {
              ctx.fillStyle = "rgba(255,210,210,0.95)";
              ctx.textAlign = "right";
              ctx.fillText(fmt(bid), xBidR - 2, y);
            }
            if (ask > 0) {
              ctx.fillStyle = "rgba(210,255,230,0.95)";
              ctx.textAlign = "left";
              ctx.fillText(fmt(ask), xAskL + 2, y);
            }
          } else if (cellMode === "delta") {
            // Left col = total vol, right col = signed delta
            const t = bid + ask;
            const d = ask - bid;
            if (t > 0) {
              ctx.fillStyle = "rgba(230,230,230,0.95)";
              ctx.textAlign = "right";
              ctx.fillText(fmt(t), xBidR - 2, y);
            }
            ctx.fillStyle = d >= 0 ? "rgba(210,255,230,0.95)" : "rgba(255,210,210,0.95)";
            ctx.textAlign = "left";
            ctx.fillText((d >= 0 ? "+" : "") + fmt(d), xAskL + 2, y);
          } else { // total
            const t = bid + ask;
            ctx.fillStyle = "rgba(230,230,230,0.95)";
            ctx.textAlign = "center";
            ctx.fillText(fmt(t), xCenter, y);
          }
        }
      }

      // Candle overlay in center
      const yO = yOfPrice(bar.o);
      const yC = yOfPrice(bar.c);
      const yH = yOfPrice(bar.h);
      const yL = yOfPrice(bar.l);
      const isUp = bar.c >= bar.o;
      const candleColor = isUp ? COLORS.up : COLORS.down;
      // Wick
      ctx.strokeStyle = candleColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xCenter, yH);
      ctx.lineTo(xCenter, yL);
      ctx.stroke();
      // Body
      const bodyTop    = Math.min(yO, yC);
      const bodyHeight = Math.max(1, Math.abs(yO - yC));
      ctx.fillStyle = candleColor;
      ctx.fillRect(xCenter - CANDLE_W / 2, bodyTop, CANDLE_W, bodyHeight);
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.strokeRect(xCenter - CANDLE_W / 2, bodyTop, CANDLE_W, bodyHeight);

      // Bar separator
      ctx.strokeStyle = COLORS.axis;
      ctx.beginPath();
      ctx.moveTo(xLeft + barW, PAD_TOP);
      ctx.lineTo(xLeft + barW, PAD_TOP + plotH);
      ctx.stroke();

    });

    // Sweep markers — single-sweep arrow + all active-sweep secondary markers
    const drawSweepMarker = (ev, primary) => {
      if (!ev || !ev.type) return;
      const age = ev.age_bars || 0;
      const targetGlobalIdx = bars.length - 1 - age;
      const slot = slotOfBar(targetGlobalIdx);
      if (slot < -1 || slot > fitN) return;       // off visible range
      const xC    = xCenterOfBar(targetGlobalIdx);
      const isHi  = ev.type === "sweep_high";
      const yWick = yOfPrice(ev.wick_extreme);
      const color = isHi ? COLORS.down : COLORS.up;
      const arrowY = isHi ? yWick - 14 : yWick + 14;
      const tipY   = isHi ? yWick - 2  : yWick + 2;
      ctx.fillStyle = color;
      ctx.globalAlpha = primary ? 1.0 : 0.55;
      ctx.beginPath();
      ctx.moveTo(xC, tipY);
      ctx.lineTo(xC - 5, arrowY);
      ctx.lineTo(xC + 5, arrowY);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1.0;
      if (primary) {
        const gran  = ev.granularity ? ev.granularity.toUpperCase() : "";
        const conf  = ev.confidence != null ? ` ${(ev.confidence * 100).toFixed(0)}%` : "";
        const patt  = ev.pattern ? ` ${ev.pattern}` : "";
        const lvl   = ev.level_label ? ` ${ev.level_label}` : "";
        const label = `SW ${gran}${lvl}${patt}${conf}`.trim();
        ctx.font = "bold 10px JetBrains Mono, monospace";
        const labelW = ctx.measureText(label).width + 8;
        const labelH = 14;
        const labelX = Math.min(Math.max(xC - labelW / 2, 2), plotW - labelW - 2);
        const labelY = isHi ? arrowY - labelH - 2 : arrowY + 4;
        ctx.fillStyle = "rgba(13,13,13,0.85)";
        ctx.fillRect(labelX, labelY, labelW, labelH);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.strokeRect(labelX, labelY, labelW, labelH);
        ctx.fillStyle = color;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(label, labelX + 4, labelY + labelH / 2);
      }
    };
    // Active registry — faint secondary arrows for non-primary sweeps
    (detections?.active_sweeps || []).forEach(ev => {
      if (ev.stale) return;
      // Reuse primary slot for the latest sweep (matches detections.sweep below)
      drawSweepMarker({
        type:          ev.sweep_type,
        wick_extreme:  ev.wick_extreme,
        age_bars:      ev.age_bars,
      }, false);
    });
    drawSweepMarker(detections?.sweep && detections.sweep.type !== "none" ? detections.sweep : null, true);

    // Absorption markers (latest bar in dataset, if it's currently visible)
    if (bars.length) {
      const latestGlobalIdx = bars.length - 1;
      const lastXC = xCenterOfBar(latestGlobalIdx);
      (detections?.absorptions || []).forEach(a => {
        const y = yOfPrice(a.price);
        if (y < PAD_TOP || y > PAD_TOP + plotH) return;
        const color = a.side === "buy" ? COLORS.up : COLORS.down;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(lastXC + barW / 2 + 6, y, 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.font = "9px JetBrains Mono, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillStyle = color;
        ctx.fillText(`ABS ${a.side} ${a.volume}`, lastXC + barW / 2 + 12, y);
      });
    }

    // Big-trade bubbles — radius scaled by volume, gradient color, value label.
    // Side colors: buy = green family, sell = red family. Outcome encoded as
    // border (pushed=bright, absorbed=dashed, exhausted=gray, pending=dotted).
    const MAX_BUBBLE_R = 26;
    const MIN_BUBBLE_R = 4;
    const bigTrades = detections?.big_trades || [];
    if (bigTrades.length) {
      const maxVol = bigTrades.reduce((m, bt) => Math.max(m, bt.volume || 0), 0.001);
      for (const bt of bigTrades) {
        const x = tsToVisibleX(bt.ts);
        const y = yOfPrice(bt.price);
        if (y < PAD_TOP || y > PAD_TOP + plotH) continue;
        if (x < -MAX_BUBBLE_R || x > plotW + MAX_BUBBLE_R) continue;
        const share = Math.sqrt((bt.volume || 0) / maxVol);   // sqrt for area scaling
        const r = MIN_BUBBLE_R + share * (MAX_BUBBLE_R - MIN_BUBBLE_R);
        const isBuy = bt.aggressor === "buy";
        const baseRgb = isBuy ? "38,166,154" : "239,83,80";
        // Radial gradient: bright center → faint edge
        const grad = ctx.createRadialGradient(x, y, 0, x, y, r);
        grad.addColorStop(0, `rgba(${baseRgb},0.95)`);
        grad.addColorStop(1, `rgba(${baseRgb},0.15)`);
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
        // Border by outcome
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
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
        // Volume value text
        if (r >= 9) {
          const v = bt.volume || 0;
          const label = v >= 1000 ? (v / 1000).toFixed(1) + "k" : v >= 100 ? v.toFixed(0) : v.toFixed(1);
          ctx.font = `bold ${Math.min(11, Math.max(8, r * 0.5))}px JetBrains Mono, monospace`;
          ctx.fillStyle = "#fff";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(label, x, y);
        }
      }
    }

    // Daily VP histogram on the right strip (aggregated from session bars).
    // Uses all loaded bars (not just visible) for accuracy.
    if (VP_WIDTH_PX > 0) {
      // Prefer server-provided full-day histogram. Falls back to client-side
      // aggregation of `bars` (legacy, limited to dashboard window).
      const vpBuckets = {};
      let vpStep = tick * 10;
      let ticksPerVpRow = 10;
      let canRender = false;
      const serverHist = dailyVp?.histogram;
      if (serverHist && serverHist.length) {
        vpStep = dailyVp.bin_size || (tick * 10);
        ticksPerVpRow = Math.max(1, Math.round(vpStep / tick));
        // Key by original price (server already provides bin centers).
        // Previous Math.floor(p/vpStep) lost the price and clustered all
        // bins at integer multiples of vpStep near zero — that's why VP
        // appeared concentrated at the top.
        for (const row of serverHist) {
          vpBuckets[row.p] = (vpBuckets[row.p] || 0) + row.v;
        }
        canRender = true;
      } else {
        const nowUTC = Date.now() / 1000;
        const istNow = new Date((nowUTC + 19800) * 1000);
        const istMidnight = Date.UTC(istNow.getUTCFullYear(), istNow.getUTCMonth(), istNow.getUTCDate()) / 1000;
        const sessionStartTs = istMidnight - 19800;
        const todayBars = bars.filter(b => b.ts >= sessionStartTs);
        if (todayBars.length) {
          const dayLow  = Math.min(...todayBars.map(b => b.l));
          const dayHigh = Math.max(...todayBars.map(b => b.h));
          const dayRange = Math.max(tick, dayHigh - dayLow);
          ticksPerVpRow = Math.max(1, Math.round((dayRange / 40) / tick));
          vpStep = tick * ticksPerVpRow;
          const vpKey = (p) => Math.floor(p / vpStep);
          for (const b of todayBars) {
            for (const lvl of (b.bid_ladder || [])) {
              if (lvl.v > 0) vpBuckets[vpKey(lvl.p)] = (vpBuckets[vpKey(lvl.p)] || 0) + lvl.v;
            }
            for (const lvl of (b.ask_ladder || [])) {
              if (lvl.v > 0) vpBuckets[vpKey(lvl.p)] = (vpBuckets[vpKey(lvl.p)] || 0) + lvl.v;
            }
            if (!b.bid_ladder && !b.ask_ladder) {
              const total = (b.bid_vol || 0) + (b.ask_vol || 0);
              if (total > 0) vpBuckets[vpKey(b.c)] = (vpBuckets[vpKey(b.c)] || 0) + total;
            }
          }
          canRender = true;
        }
      }
      if (canRender) {
        // Keys here are PRICES (server mode) or bin indices (legacy mode).
        // Distinguish by checking magnitude: any key > vpStep*10 means price.
        const rawKeys = Object.keys(vpBuckets).map(Number).sort((a, b) => a - b);
        const isPriceKey = serverHist && serverHist.length;
        if (rawKeys.length) {
          const maxV = rawKeys.reduce((m, k) => Math.max(m, vpBuckets[k]), 0.001);
          let pocKey = rawKeys[0];
          for (const k of rawKeys) if (vpBuckets[k] > vpBuckets[pocKey]) pocKey = k;
          const vpRowH = Math.max(2, pxPerTick * ticksPerVpRow);
          for (const k of rawKeys) {
            const price = isPriceKey ? k : (k + 0.5) * vpStep;
            const y = yOfPrice(price);
            if (y < PAD_TOP - vpRowH || y > PAD_TOP + plotH + vpRowH) continue;
            const w = (vpBuckets[k] / maxV) * VP_WIDTH_PX;
            const isPoc = k === pocKey;
            ctx.fillStyle = isPoc
              ? "rgba(255,215,0,0.40)"
              : "rgba(120,180,210,0.28)";
            ctx.fillRect(plotW - w, y - vpRowH / 2 + 0.5, w, vpRowH - 1);
          }
          ctx.fillStyle = "rgba(150,170,200,0.55)";
          ctx.font = "9px JetBrains Mono, monospace";
          ctx.textAlign = "right";
          ctx.textBaseline = "top";
          ctx.fillText("VP", plotW - 4, PAD_TOP + 2);
        }
      }
    }

    // Live price line (last bar close) — drawn across both plot and VP strip
    const lastBar = bars[bars.length - 1];
    if (lastBar) {
      const y = yOfPrice(lastBar.c);
      if (y >= PAD_TOP && y <= PAD_TOP + plotH) {
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = "rgba(180,180,180,0.6)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(plotW, y);
        ctx.stroke();
        ctx.restore();
        // Price tag on the price axis (right of VP strip)
        const tag = lastBar.c.toFixed(tick < 0.1 ? 2 : 1);
        ctx.font = "bold 10px JetBrains Mono, monospace";
        const tw = ctx.measureText(tag).width + 6;
        const tagX = plotW;
        ctx.fillStyle = "rgba(180,180,180,0.95)";
        ctx.fillRect(tagX, y - 7, tw, 14);
        ctx.fillStyle = "#0d0d0d";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(tag, tagX + 3, y);
      }
    }

    ctx.restore();   // end main-pane clip

    // Volume strip — total bid+ask per visible bar, scaled to strip height
    {
      const maxVol = slots.reduce(
        (m, s) => Math.max(m, ((s.bar.bid_vol || 0) + (s.bar.ask_vol || 0))), 0.001);
      ctx.strokeStyle = COLORS.axis;
      ctx.lineWidth = 1;
      ctx.strokeRect(0, volTop, plotW, VOL_STRIP_H - 2);
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, volTop, plotW, VOL_STRIP_H - 2);
      ctx.clip();
      for (const { slot, bar } of slots) {
        const xLeft = slot * barW - subBarPx;
        if (xLeft + barW < 0 || xLeft > plotW) continue;
        const total = (bar.bid_vol || 0) + (bar.ask_vol || 0);
        const h = (total / maxVol) * (VOL_STRIP_H - 6);
        const isUp = bar.c >= bar.o;
        ctx.fillStyle = isUp ? "rgba(38,166,154,0.7)" : "rgba(239,83,80,0.7)";
        const bw = Math.max(1, barW * 0.6);
        ctx.fillRect(xLeft + barW / 2 - bw / 2, volBot - h, bw, h);
      }
      ctx.restore();
      ctx.fillStyle = COLORS.textDim;
      ctx.font = "9px JetBrains Mono, monospace";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText("VOL", 4, volTop + 2);
    }

    // Plot area border
    ctx.strokeStyle = COLORS.axis;
    ctx.lineWidth = 1;
    ctx.strokeRect(0, PAD_TOP, plotW, plotH);

    // Crosshair vertical line in main pane
    if (hoverBarIdx != null) {
      const slot = hoverBarIdx - rightBarIdx + (fitN - 1);
      if (slot >= 0 && slot < fitN) {
        const xC = xCenterOfSlot(slot);
        ctx.strokeStyle = "rgba(120,150,180,0.45)";
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(xC, PAD_TOP);
        ctx.lineTo(xC, PAD_TOP + plotH);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // CVD sub-pane (mini candles below main plot) — slice by visible slots
    if (CVD_PANE_H > 0 && cvd.length) {
      const cvdSlots = slots
        .map(({ slot, barIdx }) => ({ slot, c: cvd[barIdx] }))
        .filter(x => x.c);
      if (cvdSlots.length) {
        const cvdLo = Math.min(...cvdSlots.map(x => x.c.l));
        const cvdHi = Math.max(...cvdSlots.map(x => x.c.h));
        const cvdSpan = Math.max(0.001, cvdHi - cvdLo);
        const cvdInner = CVD_PANE_H - 6;
        const cvdBot = cvdTop + CVD_PANE_H - 4;
        const cvdPan  = view.cvdPan  || 0;
        const cvdZoom = view.cvdZoom || 1;
        // Apply zoom around midpoint, then pan in pixels.
        const yOfCvd = (v) => {
          const norm = (v - (cvdLo + cvdSpan / 2)) / (cvdSpan / 2);   // [-1, 1]
          return cvdBot - cvdInner / 2 - norm * (cvdInner / 2) * cvdZoom + cvdPan;
        };
        const cvdValOfY = (y) => {
          const norm = -((y - cvdPan) - (cvdBot - cvdInner / 2)) / ((cvdInner / 2) * cvdZoom);
          return cvdLo + cvdSpan / 2 + norm * (cvdSpan / 2);
        };
        cvdPaneRef.current = { cvdLo, cvdHi, cvdSpan, cvdInner, cvdBot, yOfCvd, cvdValOfY };

        // Y-axis grid (~3 lines) with value labels on the right
        const yTicks = 3;
        ctx.font = "9px JetBrains Mono, monospace";
        ctx.textBaseline = "middle";
        for (let i = 0; i <= yTicks; i++) {
          const v = cvdLo + (cvdSpan * i / yTicks);
          const y = yOfCvd(v);
          ctx.strokeStyle = i === 0 || i === yTicks ? COLORS.axis : COLORS.grid;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(plotW, y);
          ctx.stroke();
          ctx.fillStyle = COLORS.textDim;
          ctx.textAlign = "left";
          const fmt = Math.abs(v) >= 1000 ? (v / 1000).toFixed(1) + "k" : v.toFixed(1);
          ctx.fillText(fmt, plotW + 4, y);
        }

        // Zero line emphasized
        if (cvdLo <= 0 && cvdHi >= 0) {
          const yZero = yOfCvd(0);
          ctx.strokeStyle = "rgba(180,180,180,0.55)";
          ctx.setLineDash([3, 3]);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(0, yZero);
          ctx.lineTo(plotW, yZero);
          ctx.stroke();
          ctx.setLineDash([]);
        }

        // Per-bar divergence detection (inline, no extra call):
        //  - bearish: price up, cvd down  → red ▼ above bar
        //  - bullish: price down, cvd up → green ▲ below bar
        cvdSlots.forEach(({ slot, c }, i) => {
          const xC = xCenterOfSlot(slot);
          const isUp = c.c >= c.o;
          const color = isUp ? COLORS.up : COLORS.down;
          const yH = yOfCvd(c.h);
          const yL = yOfCvd(c.l);
          const yO = yOfCvd(c.o);
          const yCv = yOfCvd(c.c);
          ctx.strokeStyle = color;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(xC, yH);
          ctx.lineTo(xC, yL);
          ctx.stroke();
          ctx.fillStyle = color;
          const bodyTop = Math.min(yO, yCv);
          const bodyH   = Math.max(1, Math.abs(yO - yCv));
          ctx.fillRect(xC - Math.min(4, barW / 4), bodyTop, Math.min(8, barW / 2), bodyH);

          // Divergence vs prior bar — skip live (in-progress) bar so the
          // triangle doesn't flicker between candles each poll.
          const isLive = bars[cvdSlots[i].slot + rightBarIdx - (fitN - 1)]?.live;
          if (i > 0 && !isLive) {
            const prev = cvdSlots[i - 1].c;
            const priceUp = c.price_close > prev.price_close;
            const priceDn = c.price_close < prev.price_close;
            const cvdUp   = c.c > prev.c;
            const cvdDn   = c.c < prev.c;
            if (priceUp && cvdDn) {
              // Bearish divergence — red triangle above bar in main pane too
              ctx.fillStyle = "rgba(239,83,80,0.95)";
              ctx.beginPath();
              const yT = PAD_TOP + 4;
              ctx.moveTo(xC, yT + 6); ctx.lineTo(xC - 4, yT); ctx.lineTo(xC + 4, yT);
              ctx.closePath();
              ctx.fill();
            } else if (priceDn && cvdUp) {
              // Bullish divergence — green triangle below bar in main pane
              ctx.fillStyle = "rgba(38,166,154,0.95)";
              ctx.beginPath();
              const yT = PAD_TOP + plotH - 4;
              ctx.moveTo(xC, yT - 6); ctx.lineTo(xC - 4, yT); ctx.lineTo(xC + 4, yT);
              ctx.closePath();
              ctx.fill();
            }
          }
        });

        // Crosshair vertical line extending into CVD pane
        if (hoverBarIdx != null) {
          const slot = hoverBarIdx - rightBarIdx + (fitN - 1);
          if (slot >= 0 && slot < fitN) {
            const xC = xCenterOfSlot(slot);
            ctx.strokeStyle = "rgba(120,150,180,0.6)";
            ctx.setLineDash([2, 3]);
            ctx.beginPath();
            ctx.moveTo(xC, cvdTop);
            ctx.lineTo(xC, cvdBot);
            ctx.stroke();
            ctx.setLineDash([]);
          }
        }

        // CVD pane label
        ctx.fillStyle = COLORS.textDim;
        ctx.font = "9px JetBrains Mono, monospace";
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        const lbl = cvdSignal && cvdSignal.pattern !== "none"
          ? `CVD ${cvdSignal.pattern}/${cvdSignal.side} ${(cvdSignal.confidence * 100).toFixed(0)}%`
          : "CVD";
        ctx.fillText(lbl, 4, cvdTop + 2);

        // CVD pane border
        ctx.strokeStyle = COLORS.axis;
        ctx.strokeRect(0, cvdTop, plotW, CVD_PANE_H - 2);
        // Resize grip (3-line glyph) at top-center of CVD pane
        ctx.fillStyle = "rgba(150,150,150,0.65)";
        const gripX = plotW / 2 - 8, gripY = cvdTop - 4;
        for (let i = 0; i < 3; i++) ctx.fillRect(gripX + i * 6, gripY, 4, 2);
      }
    }

    // Dedicated time axis — scale-aware tick interval based on visible span
    {
      const tfSec = (slots.length >= 2) ? (slots[1].bar.ts - slots[0].bar.ts) : 60;
      const visibleSpanSec = fitN * Math.max(1, tfSec);
      // Pick interval: target ~6 labels across plot
      const candidates = [60, 5*60, 15*60, 30*60, 60*60, 2*3600, 4*3600, 6*3600, 12*3600, 86400];
      let tickSec = candidates[candidates.length - 1];
      for (const c of candidates) {
        if (visibleSpanSec / c <= 10) { tickSec = c; break; }
      }
      const tAxisY = cssH - TIME_AXIS;
      ctx.fillStyle = "rgba(15,15,15,0.85)";
      ctx.fillRect(0, tAxisY, plotW, TIME_AXIS);
      ctx.strokeStyle = COLORS.axis;
      ctx.beginPath();
      ctx.moveTo(0, tAxisY);
      ctx.lineTo(plotW, tAxisY);
      ctx.stroke();
      ctx.fillStyle = COLORS.textDim;
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      let lastLabelX = -1e9;
      for (const { slot, bar } of slots) {
        if (bar.ts % tickSec !== 0) continue;
        const xC = xCenterOfSlot(slot);
        if (xC < 0 || xC > plotW) continue;
        if (xC - lastLabelX < 50) continue;     // avoid label overlap
        const d = new Date((bar.ts + IST) * 1000);
        const hh = String(d.getUTCHours()).padStart(2, "0");
        const mm = String(d.getUTCMinutes()).padStart(2, "0");
        let label = `${hh}:${mm}`;
        if (tickSec >= 86400 || (hh === "00" && mm === "00")) {
          label = `${String(d.getUTCDate()).padStart(2,"0")}/${String(d.getUTCMonth()+1).padStart(2,"0")} ${label}`;
        }
        // Tick mark
        ctx.strokeStyle = "rgba(150,150,150,0.5)";
        ctx.beginPath();
        ctx.moveTo(xC, tAxisY);
        ctx.lineTo(xC, tAxisY + 4);
        ctx.stroke();
        ctx.fillStyle = COLORS.textDim;
        ctx.fillText(label, xC, tAxisY + TIME_AXIS / 2 + 1);
        lastLabelX = xC;
      }
    }
  }, [bars, dailyVp, priorVp, detections, positions, pendingOrders, closedPositions, cvd, cvdSignal, zones, view, cellMode, selectedPosId, showTrades]);

  // Resize observer → trigger re-render
  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(() => setView(v => ({ ...v })));
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  // Wheel handling via native listener (React's onWheel is passive in React 17+).
  // Vertical wheel  -> Y zoom (pxPerTick)
  // deltaX or Shift -> horizontal pan (offsetBars)
  const barsLenRef    = useRef(bars.length);
  const maxOffsetRef  = useRef(0);
  const sliceMetaRef  = useRef({ rightBarIdx: -1, fitN: 0, barW: BAR_W_DEFAULT, subBarPx: 0 });
  barsLenRef.current  = bars.length;
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const seed = (v) => v.autoFit
      ? { ...v, pxPerTick: fitRef.current.pxPerTick, centerPrice: fitRef.current.centerPrice, autoFit: false }
      : v;
    const onWheel = (e) => {
      e.preventDefault();
      // Detect wheel inside CVD pane — route to CVD-Y pan/zoom independently.
      // Only when CVD data exists; otherwise no CVD pane is rendered.
      const rect = el.getBoundingClientRect();
      const localY = e.clientY - rect.top;
      const hasCvd = cvd && cvd.length > 0;
      const cvdH   = Math.max(30, Math.min(300, view.cvdH || 70));
      const cvdTopAbs = rect.height - TIME_AXIS - cvdH;
      const cvdBotAbs = rect.height - TIME_AXIS;
      if (hasCvd && localY >= cvdTopAbs && localY <= cvdBotAbs) {
        if (e.shiftKey || e.ctrlKey || e.metaKey) {
          const ZOOM_SENS = 0.0025;
          const factor = Math.exp(-e.deltaY * ZOOM_SENS);
          setView(v => ({ ...v, cvdZoom: Math.max(0.2, Math.min(8, (v.cvdZoom || 1) * factor)) }));
        } else {
          setView(v => ({ ...v, cvdPan: (v.cvdPan || 0) - e.deltaY * 0.5 }));
        }
        return;
      }
      const ZOOM_SENS = 0.0025;
      // Shift+wheel or Ctrl/Cmd+wheel (pinch) -> Y zoom (pxPerTick)
      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        const factor = Math.exp(-e.deltaY * ZOOM_SENS);
        setView(v => {
          const s = seed(v);
          return { ...s, pxPerTick: Math.max(0.1, Math.min(80, s.pxPerTick * factor)), yLocked: true };
        });
        return;
      }
      // Trackpad horizontal swipe → still X pan
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY) && e.deltaX !== 0) {
        const PAN_SPEED = 0.05;
        setView(v => {
          const s = seed(v);
          return { ...s, offsetBars: s.offsetBars + e.deltaX * PAN_SPEED, cvdPan: 0 };
        });
        return;
      }
      // Plain wheel → X zoom (barW), TV/Sierra-style
      const factor = Math.exp(-e.deltaY * ZOOM_SENS);
      setView(v => {
        const s = seed(v);
        return { ...s, barW: Math.max(20, Math.min(400, s.barW * factor)) };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);
  // Drag modes determined by initial cursor position:
  //  - on price-axis strip (right):   scale Y (pxPerTick)
  //  - on time-axis strip (bottom):   scale X (barW)
  //  - on plot area:                  pan X (offsetBars) + Y (centerPrice)
  const onMouseDown = (e) => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    const localX = e.clientX - rect.left;
    const localY = e.clientY - rect.top;
    // Hit-test entry markers first — click on a marker selects, no drag.
    for (const m of markerHitsRef.current) {
      if (Math.hypot(localX - m.x, localY - m.y) <= m.r) {
        setSelectedPosId(m.posId === selectedPosId ? null : m.posId);
        return;
      }
    }
    const priceAxisLeft = rect.width - PRICE_AXIS;
    const plotH = rect.height - TIME_AXIS - PAD_TOP;
    // Detect drag on CVD divider (top edge of CVD pane). cvdHeight may be 0.
    const cvdHeight = (cvd && cvd.length) ? Math.max(30, Math.min(300, view.cvdH || 70)) : 0;
    const cvdTopY   = PAD_TOP + (plotH - cvdHeight);   // top edge of CVD pane
    const dividerHit = cvdHeight > 0 && localY >= cvdTopY - 6 && localY <= cvdTopY + 4 && localX < priceAxisLeft;
    let mode = "pan";
    if (dividerHit)                       mode = "resizeCvd";
    else if (localX >= priceAxisLeft)     mode = "scaleY";
    else if (localY >= PAD_TOP + plotH)   mode = "scaleX";
    // Seed from displayed (fit) values when autoFit is on
    const seedPx = view.autoFit ? fitRef.current.pxPerTick   : view.pxPerTick;
    const seedC  = view.autoFit ? fitRef.current.centerPrice : view.centerPrice;
    if (view.autoFit) {
      setView(v => ({ ...v, pxPerTick: seedPx, centerPrice: seedC, autoFit: false }));
    }
    dragRef.current = {
      mode,
      x:          e.clientX,
      y:          e.clientY,
      pxPerTick:  seedPx,
      barW:       view.barW,
      centerPrice: seedC,
      offsetBars: view.offsetBars,
      cvdH:       view.cvdH || 70,
      cvdPan:     view.cvdPan || 0,
      cvdZoom:    view.cvdZoom || 1,
    };
  };
  const onMouseMove = (e) => {
    // Always update crosshair index (independent of drag state)
    const wrap = wrapRef.current;
    if (wrap && bars.length) {
      const rect = wrap.getBoundingClientRect();
      const localX = e.clientX - rect.left;
      const localY = e.clientY - rect.top;
      const meta = sliceMetaRef.current;
      const plotWGuess = rect.width - PRICE_AXIS - VP_WIDTH_PX;
      if (localY >= PAD_TOP && localY <= rect.height - TIME_AXIS && localX >= 0 && localX <= plotWGuess) {
        // Reverse: xLeft = (i - leftSkip) * barW - subBarPx
        // Reverse: xLeft of slot s = s * barW - subBarPx → slot = floor((localX + subBarPx) / barW)
        const slot = Math.floor((localX + meta.subBarPx) / meta.barW);
        const globalIdx = meta.rightBarIdx - (meta.fitN - 1 - slot);
        if (globalIdx >= 0 && globalIdx < bars.length) {
          setHoverBarIdx(globalIdx);
        }
      } else {
        setHoverBarIdx(null);
      }
    }
    const drag = dragRef.current;
    if (!drag || !bars.length) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;

    if (drag.mode === "scaleY") {
      // Drag down → zoom in (bigger pxPerTick), drag up → zoom out
      const factor = Math.exp(dy * 0.01);
      setView(v => ({
        ...v,
        pxPerTick: Math.max(0.1, Math.min(80, drag.pxPerTick * factor)),
        yLocked: true,
      }));
      return;
    }
    if (drag.mode === "scaleX") {
      // Drag right → wider bars, drag left → narrower
      const factor = Math.exp(dx * 0.01);
      setView(v => ({
        ...v,
        barW: Math.max(30, Math.min(300, drag.barW * factor)),
      }));
      return;
    }
    if (drag.mode === "resizeCvd") {
      // Drag up → bigger CVD pane (steals from main plot)
      setView(v => ({
        ...v,
        cvdH: Math.max(30, Math.min(300, drag.cvdH - dy)),
      }));
      return;
    }
    // pan mode
    const tick = inferTickSize(bars.slice(-20));
    const pxPerPrice = drag.pxPerTick / tick;
    const refSlice = bars.slice(-1);
    const baseCenter = drag.centerPrice ?? (
      (Math.min(...refSlice.map(b => b.l)) + Math.max(...refSlice.map(b => b.h))) / 2
    );
    // Drag right → reveal older bars (offset increases). 1:1 pixel mapping.
    // X clamped by draw effect; Y clamped to data range with margin so a
    // shallow zoom doesn't translate tiny dy into multi-thousand price drift.
    const dBars = dx / view.barW;
    const allLow  = Math.min(...bars.map(b => b.l));
    const allHigh = Math.max(...bars.map(b => b.h));
    const yMargin = Math.max(tick * 50, (allHigh - allLow) * 0.5);
    const rawCenter = baseCenter + dy / pxPerPrice;
    const clampedCenter = Math.max(allLow - yMargin, Math.min(allHigh + yMargin, rawCenter));
    // Only lock Y if user moved vertically more than a small threshold —
    // otherwise X-pan stays in auto-follow mode.
    const yMoved = Math.abs(dy) > 4;
    setView(v => ({
      ...v,
      centerPrice: yMoved ? clampedCenter : v.centerPrice,
      offsetBars:  drag.offsetBars + dBars,
      yLocked:     v.yLocked || yMoved,
      // When user pans the main chart, CVD should follow the visible slice —
      // clear any earlier manual CVD pan/zoom so the new slice auto-fits.
      cvdPan:      Math.abs(dBars) > 0.5 ? 0 : v.cvdPan,
      cvdZoom:     Math.abs(dBars) > 0.5 ? 1 : v.cvdZoom,
    }));
  };
  const onMouseUp = () => { dragRef.current = null; };

  const reset = () => setView({
    pxPerTick: 14, centerPrice: null,
    barW: BAR_W_DEFAULT, offsetBars: 0, autoFit: true, yLocked: false, cvdPan: 0, cvdZoom: 1,
  });

  // ── side panel data ────────────────────────────────────────────────────────
  const hoverBar = hoverBarIdx != null ? bars[hoverBarIdx] : null;
  const fmtNum = (n, dp = 2) => Number.isFinite(n) ? n.toFixed(dp) : "—";
  const istHHMM = (ts) => {
    const d = new Date((ts + 19800) * 1000);
    return `${String(d.getUTCHours()).padStart(2,"0")}:${String(d.getUTCMinutes()).padStart(2,"0")}`;
  };
  const firedOnHoverBar = (() => {
    if (!hoverBar) return [];
    const lines = [];
    // sweep age_bars relative to last
    const ageOfHover = (bars.length - 1) - hoverBarIdx;
    (detections?.active_sweeps || []).forEach(ev => {
      if (!ev.stale && ev.age_bars === ageOfHover) {
        lines.push(`sweep:${ev.sweep_type === "sweep_high" ? "HIGH" : "LOW"}(${ev.level_label || "?"})`);
      }
    });
    if (hoverBarIdx === bars.length - 1) {
      (detections?.absorptions || []).forEach(a => lines.push(`ABS ${a.side}@${fmtNum(a.price, 2)}`));
    }
    (detections?.fvgs || []).forEach(f => {
      if (f.formed_at_ts === hoverBar.ts) lines.push(`FVG${f.side === "bull" ? "↑" : "↓"}(formed)`);
    });
    (detections?.big_trades || []).forEach(bt => {
      if (bt.ts === hoverBar.ts) lines.push(`BIG ${bt.aggressor} ${bt.volume}/${bt.outcome}`);
    });
    return lines;
  })();

  const badgeBox = {
    position: "absolute",
    background: "rgba(13,13,13,0.85)",
    border: "1px solid #2a2a2a",
    borderRadius: 3,
    padding: "4px 6px",
    fontFamily: "JetBrains Mono, monospace",
    fontSize: 10,
    color: "#bbb",
    lineHeight: 1.45,
    pointerEvents: "none",
    zIndex: 9,
  };

  return (
    <div
      ref={wrapRef}
      style={{ width: "100%", height: "100%", position: "relative", background: COLORS.bg }}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={() => { onMouseUp(); setHoverBarIdx(null); }}
    >
      <canvas ref={canvasRef} style={{ display: "block", cursor: dragRef.current ? "grabbing" : "grab" }} />

      {/* Wave / day_type badge */}
      {(detections?.wave || detections?.day_type) && (
        <div style={{ ...badgeBox, top: 6, left: 6, minWidth: 180 }}>
          {detections?.wave && (
            <div>
              <span style={{ color: "#888" }}>WAVE </span>
              <span style={{ color: "#ddd" }}>{detections.wave.wave_label}</span>
              <span style={{ color: "#666" }}> {(detections.wave.confidence * 100).toFixed(0)}%</span>
            </div>
          )}
          {detections?.day_type && (
            <div>
              <span style={{ color: "#888" }}>DAY </span>
              <span style={{ color: "#ddd" }}>{detections.day_type.type}</span>
              <span style={{ color: "#666" }}> grid={detections.day_type.grid_mode}</span>
            </div>
          )}
        </div>
      )}

      {/* Pinned crosshair readout */}
      {hoverBar && (
        <div style={{ ...badgeBox, top: 56, left: 6, minWidth: 320 }}>
          <div>
            <span style={{ color: "#888" }}>{istHHMM(hoverBar.ts)} IST</span>
            <span style={{ color: "#ddd" }}> O {fmtNum(hoverBar.o, 2)}  H {fmtNum(hoverBar.h, 2)}  L {fmtNum(hoverBar.l, 2)}  C {fmtNum(hoverBar.c, 2)}</span>
          </div>
          <div>
            <span style={{ color: (hoverBar.delta || 0) >= 0 ? COLORS.up : COLORS.down }}>
              Δ {(hoverBar.delta || 0) >= 0 ? "+" : ""}{fmtNum(hoverBar.delta, 2)}
            </span>
            <span style={{ color: "#888" }}>  Σbid {fmtNum(hoverBar.bid_vol, 2)}  Σask {fmtNum(hoverBar.ask_vol, 2)}  POC {fmtNum(hoverBar.poc, 2)}</span>
          </div>
          {(() => {
            const c = (cvd || [])[hoverBarIdx];
            if (!c) return null;
            return (
              <div style={{ color: "#888" }}>
                <span>CVD</span>
                <span style={{ color: c.c >= c.o ? COLORS.up : COLORS.down }}>
                  {" "}{fmtNum(c.c, 2)}
                </span>
                <span>  Δ </span>
                <span style={{ color: (c.delta || 0) >= 0 ? COLORS.up : COLORS.down }}>
                  {(c.delta || 0) >= 0 ? "+" : ""}{fmtNum(c.delta, 2)}
                </span>
              </div>
            );
          })()}
          {firedOnHoverBar.length > 0 && (
            <div style={{ color: "#ffd700", marginTop: 2 }}>FIRED: {firedOnHoverBar.join("  ")}</div>
          )}
        </div>
      )}

      {/* M2 vote strip */}
      {latestM2?.votes?.length > 0 && (
        <div style={{
          ...badgeBox,
          top: 36, right: PRICE_AXIS + 8,
          padding: "3px 5px",
          display: "flex", gap: 3,
          pointerEvents: "auto",
        }}>
          {latestM2.votes.map((v, i) => {
            const dir = v.direction ?? 0;
            const str = Math.max(0.15, Math.min(1, v.strength ?? 0.5));
            const color = dir >= 0
              ? `rgba(38,166,154,${0.25 + str * 0.7})`
              : `rgba(239,83,80,${0.25 + str * 0.7})`;
            const shortLabel = ({
              cvd: "cvd", vp_shape: "vps", vp_position: "vpp",
              fvg: "fvg", wave: "wv", sweep: "sw", confirmation: "cf",
            })[v.module] || v.module.slice(0, 3);
            return (
              <div
                key={i}
                title={`${v.module}: ${v.reason || ""} (dir ${dir.toFixed(2)}, str ${(v.strength ?? 0).toFixed(2)})`}
                style={{
                  width: 22, height: 22, background: color,
                  border: "1px solid rgba(0,0,0,0.4)", borderRadius: 2,
                  fontSize: 8, color: "#000", textAlign: "center",
                  lineHeight: "22px", fontWeight: 600, cursor: "default",
                }}
              >
                {shortLabel}
              </div>
            );
          })}
        </div>
      )}

      {/* Decision popup for the selected position */}
      {selectedPosId && (() => {
        const pos = (positions || []).find(p => p.position_id === selectedPosId);
        if (!pos) return null;
        const r = (n, dp = 2) => Number.isFinite(n) ? n.toFixed(dp) : "—";
        const openedIst = (() => {
          if (!pos.opened_ts) return "?";
          const d = new Date((pos.opened_ts + 19800) * 1000);
          return `${String(d.getUTCHours()).padStart(2,"0")}:${String(d.getUTCMinutes()).padStart(2,"0")}`;
        })();
        const sideColor = pos.side === "long" ? COLORS.up : COLORS.down;
        return (
          <div style={{
            ...badgeBox,
            top: 56, right: PRICE_AXIS + 8,
            minWidth: 280, maxWidth: 380,
            pointerEvents: "auto",
            fontSize: 11, lineHeight: 1.55,
            padding: "8px 10px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span style={{ color: sideColor, fontWeight: 700 }}>
                {pos.side === "long" ? "▲ LONG" : "▼ SHORT"} {symbol}
              </span>
              <span
                style={{ color: "#888", cursor: "pointer" }}
                onClick={() => setSelectedPosId(null)}
                title="close"
              >×</span>
            </div>
            <div style={{ color: "#999" }}>
              opened {openedIst} IST · id {pos.position_id.slice(0, 8)}
            </div>
            <div style={{ marginTop: 5 }}>
              <span style={{ color: "#888" }}>avg entry </span><span>{r(pos.avg_entry)}</span>
              <span style={{ color: "#888" }}>  TP </span><span style={{ color: "#ffd700" }}>{r(pos.take_profit)}</span>
              <span style={{ color: "#888" }}>  SL </span><span style={{ color: COLORS.down }}>{r(pos.stop_loss)}</span>
            </div>
            <div>
              <span style={{ color: "#888" }}>filled </span>
              <span style={{ color: "#ddd" }}>{pos.legs_filled}/{(pos.legs || pos.leg_prices || []).length}</span>
              {pos.total_lots != null && (
                <>
                  <span style={{ color: "#888" }}>  Σqty </span>
                  <span style={{ color: "#ddd" }}>{r(pos.total_lots, 2)}</span>
                </>
              )}
            </div>
            <div style={{ marginTop: 4, fontSize: 10 }}>
              <div style={{ color: "#888" }}>FILLED LEGS</div>
              {(pos.legs && pos.legs.length ? pos.legs : (pos.leg_prices || []).map((p, i) => ({
                leg: i + 1, entry: p, lots: null,
              }))).map((leg, i) => (
                <div key={i} style={{ color: "#ccc" }}>
                  <span style={{ color: "#777" }}>L{leg.leg}</span>
                  <span style={{ color: "#888" }}> px </span><span>{r(leg.entry)}</span>
                  {leg.lots != null && <>
                    <span style={{ color: "#888" }}>  qty </span>
                    <span>{r(leg.lots, 2)}</span>
                  </>}
                </div>
              ))}
            </div>
            {(() => {
              const pendingForPos = (pendingOrders || []).filter(p => p.position_id === pos.position_id);
              if (!pendingForPos.length) return null;
              return (
                <div style={{ marginTop: 4, fontSize: 10 }}>
                  <div style={{ color: "#888" }}>PENDING LEGS</div>
                  {pendingForPos.map(po => (
                    <div key={po.pending_id} style={{ color: "#bbb" }}>
                      <span style={{ color: "#777" }}>L{po.leg_idx}</span>
                      <span style={{ color: "#888" }}> px </span><span>{r(po.limit_price)}</span>
                      <span style={{ color: "#888" }}>  qty </span><span>{r(po.lots, 2)}</span>
                      <span style={{ color: "#888" }}>  TP </span><span style={{ color: "#ffd700" }}>{r(po.tp)}</span>
                    </div>
                  ))}
                </div>
              );
            })()}
            {latestM2 && (
              <div style={{ marginTop: 6, borderTop: "1px solid #2a2a2a", paddingTop: 5 }}>
                <div><span style={{ color: "#888" }}>M2 latest </span>
                  <span style={{ color: latestM2.side === "long" ? COLORS.up : latestM2.side === "short" ? COLORS.down : "#888" }}>
                    {latestM2.side}
                  </span>
                  <span style={{ color: "#888" }}> score </span>{r(latestM2.score, 3)}
                  <span style={{ color: "#888" }}> str </span>{latestM2.bias_strength}
                </div>
                {latestM2.note && <div style={{ color: "#aaa", marginTop: 2 }}>{latestM2.note}</div>}
                <div style={{ marginTop: 3 }}>
                  {(latestM2.votes || []).map((v, i) => (
                    <div key={i} style={{ color: "#aaa", fontSize: 10 }}>
                      <span style={{ color: v.direction >= 0 ? COLORS.up : COLORS.down }}>
                        {v.direction >= 0 ? "+" : ""}{r(v.direction, 2)}
                      </span>
                      <span style={{ color: "#666" }}> ×{r(v.strength, 2)} </span>
                      <span style={{ color: "#bbb" }}>{v.module}</span>
                      <span style={{ color: "#777" }}>: {v.reason}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      <button
        onClick={reset}
        title="Reset"
        style={{
          position: "absolute", top: 6, right: PRICE_AXIS + 8,
          background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
          color: "#888", fontSize: 11, padding: "2px 7px", cursor: "pointer",
          fontFamily: "monospace", borderRadius: 3, zIndex: 10, lineHeight: 1.4,
        }}
      >
        ⊡
      </button>
      <button
        onClick={() => setShowTrades(s => !s)}
        title="Toggle trade history overlay"
        style={{
          position: "absolute", top: 6, right: PRICE_AXIS + 84,
          background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
          color: showTrades ? "#ffd700" : "#666",
          fontSize: 11, padding: "2px 7px", cursor: "pointer",
          fontFamily: "monospace", borderRadius: 3, zIndex: 10, lineHeight: 1.4,
        }}
      >
        {showTrades ? "TRADES" : "trades"}
      </button>
      <button
        onClick={() => setCellMode(m =>
          m === "bidask" ? "delta" : m === "delta" ? "total" : "bidask"
        )}
        title="Cell display mode"
        style={{
          position: "absolute", top: 6, right: PRICE_AXIS + 38,
          background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
          color: "#ccc", fontSize: 11, padding: "2px 7px", cursor: "pointer",
          fontFamily: "monospace", borderRadius: 3, zIndex: 10, lineHeight: 1.4,
        }}
      >
        {cellMode === "bidask" ? "BID×ASK" : cellMode === "delta" ? "Δ" : "ΣVOL"}
      </button>
    </div>
  );
}
