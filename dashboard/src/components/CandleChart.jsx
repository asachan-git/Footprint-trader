import { useEffect, useMemo, useRef, useState } from "react";
import { createChart, CrosshairMode, LineStyle, PriceScaleMode } from "lightweight-charts";

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

// True volume-at-price profile from bid/ask ladders.
//   vpMode "visible" → only bars in the visible logical range (zoom-scoped,
//   bounded cost); "full" → entire loaded window.
// Computes POC, 70% value area, and HVN/LVN by volume percentile — all from the
// actual per-price ladders (not a close-price approximation). Falls back to
// close-bucketing only when bars carry no ladders (synthetic history).
function drawVP(canvas, chart, candleSeries, dailyVp, bars, vpMode = "visible") {
  if (!canvas || !chart || !candleSeries || !bars.length) return;

  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;

  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  const VP_MAX_W = Math.round(W * 0.16);

  // Scope bars: visible-range slice (cost bounded by zoom) or full window.
  let slice = bars;
  if (vpMode !== "full") {
    const lr = chart.timeScale().getVisibleLogicalRange();
    if (lr) {
      const from = Math.max(0, Math.floor(lr.from));
      const to   = Math.min(bars.length - 1, Math.ceil(lr.to));
      if (to >= from) slice = bars.slice(from, to + 1);
    }
  }

  // Aggregate real volume-at-price from ladders.
  const prof = new Map();   // price -> {price, bid, ask}
  let haveLadder = false;
  const add = (p, bid, ask) => {
    const e = prof.get(p) || { price: p, bid: 0, ask: 0 };
    e.bid += bid; e.ask += ask; prof.set(p, e);
  };
  for (const b of slice) {
    const nb = (b.bid_ladder?.length || 0) + (b.ask_ladder?.length || 0);
    if (nb > 0) haveLadder = true;
    (b.bid_ladder || []).forEach(l => add(l.p, l.v, 0));
    (b.ask_ladder || []).forEach(l => add(l.p, 0, l.v));
  }
  if (!haveLadder) {
    // Synthetic bars: no ladders — bucket by close price.
    const poc0 = dailyVp?.poc || slice[0]?.c || 1;
    const step0 = Math.max(poc0 * 0.0003, 0.01);
    for (const b of slice) {
      add(Math.round(b.c / step0) * step0, b.bid_vol || 0, b.ask_vol || 0);
    }
  }

  const rows = [...prof.values()].sort((a, b) => a.price - b.price);
  if (!rows.length) return;
  const totals = rows.map(r => r.bid + r.ask);
  const maxVol = Math.max(...totals, 1);
  const grand  = totals.reduce((s, v) => s + v, 0) || 1;

  // POC + 70% value area (expand around POC toward the larger neighbour).
  let pocIdx = 0;
  for (let i = 1; i < rows.length; i++) if (totals[i] > totals[pocIdx]) pocIdx = i;
  let lo = pocIdx, hi = pocIdx, vaVol = totals[pocIdx];
  const target = grand * 0.7;
  while (vaVol < target && (lo > 0 || hi < rows.length - 1)) {
    const below = lo > 0 ? totals[lo - 1] : -1;
    const above = hi < rows.length - 1 ? totals[hi + 1] : -1;
    if (above >= below) { hi++; vaVol += Math.max(0, above); }
    else                { lo--; vaVol += Math.max(0, below); }
  }
  const pocPrice = rows[pocIdx].price;

  // HVN/LVN thresholds by volume percentile of the profile.
  const sortedTot = [...totals].sort((a, b) => a - b);
  const pctl = (q) => sortedTot[Math.min(sortedTot.length - 1, Math.floor(q * sortedTot.length))];
  const hvnT = pctl(0.80), lvnT = pctl(0.20);

  // Row height from the smallest positive price step.
  let step = Infinity;
  for (let i = 1; i < rows.length; i++) {
    const d = rows[i].price - rows[i - 1].price;
    if (d > 0 && d < step) step = d;
  }
  if (!isFinite(step)) step = Math.max(pocPrice * 0.0001, 0.01);
  const yA = candleSeries.priceToCoordinate(pocPrice);
  const yB = candleSeries.priceToCoordinate(pocPrice + step);
  const cellH = (yA != null && yB != null) ? Math.max(1, Math.abs(yA - yB)) : 2;

  // Value-area band background.
  const yVaTop = candleSeries.priceToCoordinate(rows[hi].price);
  const yVaBot = candleSeries.priceToCoordinate(rows[lo].price);
  if (yVaTop != null && yVaBot != null) {
    ctx.fillStyle = "rgba(120,140,180,0.05)";
    ctx.fillRect(0, Math.min(yVaTop, yVaBot) - cellH / 2, VP_MAX_W, Math.abs(yVaBot - yVaTop) + cellH);
  }

  for (let i = 0; i < rows.length; i++) {
    const r = rows[i], total = totals[i];
    if (total <= 0) continue;
    const y = candleSeries.priceToCoordinate(r.price);
    if (y === null || y < -cellH || y > H + cellH) continue;
    // HVN/LVN tint
    if (total >= hvnT)      { ctx.fillStyle = "rgba(66,165,245,0.05)"; ctx.fillRect(0, y - cellH / 2, VP_MAX_W, cellH); }
    else if (total <= lvnT) { ctx.fillStyle = "rgba(255,152,0,0.05)"; ctx.fillRect(0, y - cellH / 2, VP_MAX_W, cellH); }

    const totalW = (total / maxVol) * VP_MAX_W;
    const bidW   = (r.bid / total) * totalW;
    const askW   = totalW - bidW;
    const isPoc  = i === pocIdx;
    const inVA   = i >= lo && i <= hi;
    const rh     = Math.max(1, cellH * 0.9);
    const aBid   = isPoc ? 0.7 : inVA ? 0.5 : 0.28;
    const aAsk   = isPoc ? 0.7 : inVA ? 0.55 : 0.30;
    ctx.fillStyle = `rgba(239,83,80,${aBid})`;
    ctx.fillRect(0, y - rh / 2, bidW, rh);
    ctx.fillStyle = `rgba(38,166,154,${aAsk})`;
    ctx.fillRect(bidW, y - rh / 2, askW, rh);
  }

  // POC line + value-area edge ticks across the VP strip.
  const yPoc = candleSeries.priceToCoordinate(pocPrice);
  if (yPoc != null) {
    ctx.strokeStyle = "rgba(255,215,0,0.85)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, yPoc); ctx.lineTo(VP_MAX_W, yPoc); ctx.stroke();
  }
  for (const yy of [yVaTop, yVaBot]) {
    if (yy == null) continue;
    ctx.strokeStyle = "rgba(120,140,180,0.4)"; ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(VP_MAX_W, yy); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "rgba(255,215,0,0.6)"; ctx.font = "9px JetBrains Mono, monospace";
  ctx.textAlign = "left"; ctx.textBaseline = "bottom";
  ctx.fillText(`VP·${vpMode === "full" ? "full" : "vis"} POC ${pocPrice.toFixed(2)}`, 3, H - 3);
}

// ── Trade position boxes (TradingView long/short tool look) ──────────────────
// Green reward zone entry→TP, red risk zone entry→SL, entry line + RR label.
// Open trades (no exit_ts) extend to the right edge.
function drawTradeBoxes(canvas, chart, candleSeries, trades, bars) {
  if (!canvas || !chart || !candleSeries) return;
  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  if (!trades?.length) return;

  const ts = chart.timeScale();
  const lastTs = bars?.length ? bars[bars.length - 1].ts : null;

  for (const t of trades) {
    if (t.entry == null || t.entry_ts == null) continue;
    const x1 = ts.timeToCoordinate(t.entry_ts);
    if (x1 == null) continue;
    let x2 = t.exit_ts ? ts.timeToCoordinate(t.exit_ts)
                       : (lastTs != null ? ts.timeToCoordinate(lastTs) : null);
    if (x2 == null) x2 = W;                 // open & beyond right edge → extend
    if (x2 <= x1) x2 = x1 + 3;
    const bw = Math.max(3, x2 - x1);

    const yE  = candleSeries.priceToCoordinate(t.entry);
    if (yE == null) continue;
    const ySL = t.sl != null ? candleSeries.priceToCoordinate(t.sl) : null;
    const yTP = t.tp != null ? candleSeries.priceToCoordinate(t.tp) : null;
    const col = STRAT_COLORS[t.strategy] || "#fff";
    const cyc = !!t.is_cycle;   // cycles drawn dashed + lighter

    const aFill = cyc ? 0.12 : 0.24;   // cycles fainter
    // reward zone (entry → TP) — green gradient, lighter at entry
    if (yTP != null) {
      const g = ctx.createLinearGradient(0, yE, 0, yTP);
      g.addColorStop(0, "rgba(38,166,154,0.04)");
      g.addColorStop(1, `rgba(38,166,154,${aFill})`);
      ctx.fillStyle = g;
      ctx.fillRect(x1, Math.min(yE, yTP), bw, Math.abs(yTP - yE));
    }
    // risk zone (entry → SL) — red gradient
    if (ySL != null) {
      const g = ctx.createLinearGradient(0, yE, 0, ySL);
      g.addColorStop(0, "rgba(239,83,80,0.04)");
      g.addColorStop(1, `rgba(239,83,80,${aFill})`);
      ctx.fillStyle = g;
      ctx.fillRect(x1, Math.min(yE, ySL), bw, Math.abs(ySL - yE));
    }
    // zone edges (cycles dashed)
    ctx.lineWidth = 1;
    ctx.setLineDash(cyc ? [4, 3] : []);
    if (yTP != null) { ctx.strokeStyle = "rgba(38,166,154,0.65)"; ctx.beginPath(); ctx.moveTo(x1, yTP); ctx.lineTo(x2, yTP); ctx.stroke(); }
    if (ySL != null) { ctx.strokeStyle = "rgba(239,83,80,0.65)"; ctx.beginPath(); ctx.moveTo(x1, ySL); ctx.lineTo(x2, ySL); ctx.stroke(); }
    // left (entry) + right (exit) vertical bounds spanning SL↔TP — frames the box
    const yTop = Math.min(ySL ?? yE, yTP ?? yE), yBot = Math.max(ySL ?? yE, yTP ?? yE);
    ctx.strokeStyle = "rgba(150,150,150,0.45)";
    ctx.beginPath(); ctx.moveTo(x1, yTop); ctx.lineTo(x1, yBot); ctx.stroke();           // entry edge
    if (!t.open) { ctx.beginPath(); ctx.moveTo(x2, yTop); ctx.lineTo(x2, yBot); ctx.stroke(); } // exit edge
    // entry line + dot
    ctx.strokeStyle = col; ctx.lineWidth = cyc ? 1 : 1.5;
    ctx.beginPath(); ctx.moveTo(x1, yE); ctx.lineTo(x2, yE); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x1, yE, 3, 0, 2 * Math.PI); ctx.fill();

    // EXIT marker — where the trade actually closed (win=green / loss=red),
    // plus a path line entry→exit showing the outcome.
    const yX = t.exit_price != null ? candleSeries.priceToCoordinate(t.exit_price) : null;
    if (!t.open && yX != null) {
      const won = (t.r ?? t.pnl ?? 0) > 0;
      const xc = won ? COLORS.up : COLORS.down;
      ctx.strokeStyle = xc; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
      ctx.beginPath(); ctx.moveTo(x1, yE); ctx.lineTo(x2, yX); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = xc;
      ctx.beginPath();                                   // diamond at close
      ctx.moveTo(x2, yX - 3.5); ctx.lineTo(x2 + 3.5, yX);
      ctx.lineTo(x2, yX + 3.5); ctx.lineTo(x2 - 3.5, yX); ctx.closePath(); ctx.fill();
      // outcome label at exit
      const out = t.r != null ? `${t.r >= 0 ? "+" : ""}${t.r.toFixed(2)}R`
                : t.pnl != null ? `${t.pnl >= 0 ? "+" : ""}${(+t.pnl).toFixed(2)}` : "";
      if (out) {
        ctx.font = "10px JetBrains Mono, monospace";
        const ow = ctx.measureText(out).width;
        const ox = Math.min(x2 + 6, W - ow - 4);
        ctx.fillStyle = "rgba(13,13,13,0.82)"; ctx.fillRect(ox - 2, yX - 6, ow + 4, 12);
        ctx.fillStyle = xc; ctx.fillText(out, ox, yX + 3);
      }
    }

    // RR label at entry
    let rr = null;
    if (t.sl != null && t.tp != null && t.entry !== t.sl)
      rr = Math.abs(t.tp - t.entry) / Math.abs(t.entry - t.sl);
    const tag = STRAT_TAG[t.strategy] || "?";
    const label = `${t.side === "long" ? "▲" : "▼"}${tag}`
                + `${cyc && t.cycle_num != null ? `#${t.cycle_num}` : ""}`
                + `${rr != null ? `  RR ${rr.toFixed(2)}` : ""}`
                + `${t.open ? "  ●" : ""}`;
    ctx.font = "10px JetBrains Mono, monospace";
    const tw = ctx.measureText(label).width;
    const lx = Math.min(x1 + 4, W - tw - 6);
    const ly = yE - 5;
    ctx.fillStyle = "rgba(13,13,13,0.82)";
    ctx.fillRect(lx - 3, ly - 9, tw + 6, 12);
    ctx.fillStyle = col;
    ctx.fillText(label, lx, ly);
  }
}

// ── CVD divergence overlay ───────────────────────────────────────────────────
// Marks price/CVD regular divergences at swing pivots (from backend
// scan_divergences). bear ▽ red above the swing high, bull △ green below the
// swing low. hitsOut collects {x,y,r,d} for hover tooltips.
function drawCVDDivergence(canvas, chart, candleSeries, divs, bars, show, hitsOut) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);
  if (hitsOut) hitsOut.length = 0;
  if (!show || !divs?.length) return;

  const ts = chart.timeScale();
  for (const d of divs) {
    const x = ts.timeToCoordinate(d.ts);
    if (x == null) continue;
    const y = candleSeries.priceToCoordinate(d.price);
    if (y == null) continue;
    const bear = d.type === "bear";
    const col = bear ? COLORS.down : COLORS.up;
    const dir = bear ? -1 : 1;             // bear above (−), bull below (+)
    const ty = bear ? y - 16 : y + 16;     // triangle tip offset from price
    const s = 6;                            // half-width
    ctx.beginPath();
    if (bear) { ctx.moveTo(x, ty + s); ctx.lineTo(x - s, ty - s); ctx.lineTo(x + s, ty - s); }
    else      { ctx.moveTo(x, ty - s); ctx.lineTo(x - s, ty + s); ctx.lineTo(x + s, ty + s); }
    ctx.closePath();
    ctx.fillStyle = col;
    ctx.globalAlpha = 0.9;
    ctx.fill();
    ctx.globalAlpha = 1;
    // connector to the price extreme
    ctx.strokeStyle = col;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(x, ty + dir * s); ctx.lineTo(x, y); ctx.stroke();
    ctx.setLineDash([]);
    if (hitsOut) hitsOut.push({ x, y: ty, r: 9, d });
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

const STRAT_COLORS = {
  coup:      "#ff9800",
  democracy: "#42a5f5",
  republic:  "#ab47bc",
  m1:        "#26a69a",
  m2:        "#ffca28",
  reversal:  "#26c6da",
};
const STRAT_TAG = { coup: "C", democracy: "D", republic: "R", m1: "1", m2: "2", reversal: "⚑" };

// ── Indicator math (chart-only; no server coupling) ──────────────────────────

const IST_SEC = 19800;
// UTC-midnight session bucket (system uses SESSION_START_UTC=0 for both symbols).
const _sessionDay = (ts) => Math.floor(ts / 86400);

// Session-anchored VWAP + volume-weighted σ bands. Resets each UTC day.
// Returns parallel arrays keyed to bars: {vwap, u1, l1, u2, l2}.
function computeVWAP(bars) {
  const out = { vwap: [], u1: [], l1: [], u2: [], l2: [] };
  let day = null, cumPV = 0, cumV = 0, cumP2V = 0;
  for (const b of bars) {
    const d = _sessionDay(b.ts);
    if (d !== day) { day = d; cumPV = cumV = cumP2V = 0; }
    const tp = (b.h + b.l + b.c) / 3;
    const v = (b.bid_vol || 0) + (b.ask_vol || 0);
    cumPV += tp * v; cumV += v; cumP2V += tp * tp * v;
    const vwap = cumV > 0 ? cumPV / cumV : tp;
    const variance = cumV > 0 ? Math.max(0, cumP2V / cumV - vwap * vwap) : 0;
    const sd = Math.sqrt(variance);
    out.vwap.push({ time: b.ts, value: vwap });
    out.u1.push({ time: b.ts, value: vwap + sd });
    out.l1.push({ time: b.ts, value: vwap - sd });
    out.u2.push({ time: b.ts, value: vwap + 2 * sd });
    out.l2.push({ time: b.ts, value: vwap - 2 * sd });
  }
  return out;
}

// SuperTrend — ATR-based trailing stop that flips with trend. The single most
// useful "visual TSL": line sits below price in uptrend (green), above in
// downtrend (red). period=ATR length, mult=band width.
function computeSuperTrend(bars, period = 10, mult = 3) {
  const n = bars.length;
  if (n < period + 1) return [];
  // Wilder ATR
  const tr = new Array(n).fill(0);
  for (let i = 0; i < n; i++) {
    const b = bars[i];
    if (i === 0) { tr[i] = b.h - b.l; continue; }
    const pc = bars[i - 1].c;
    tr[i] = Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc));
  }
  const atr = new Array(n).fill(0);
  let seed = 0;
  for (let i = 1; i <= period; i++) seed += tr[i];
  atr[period] = seed / period;
  for (let i = period + 1; i < n; i++) atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period;

  const out = [];
  let prevUpper = 0, prevLower = 0, prevST = 0, prevDir = 1; // 1=up, -1=down
  for (let i = period; i < n; i++) {
    const b = bars[i];
    const mid = (b.h + b.l) / 2;
    let upper = mid + mult * atr[i];
    let lower = mid - mult * atr[i];
    if (i > period) {
      upper = (upper < prevUpper || bars[i - 1].c > prevUpper) ? upper : prevUpper;
      lower = (lower > prevLower || bars[i - 1].c < prevLower) ? lower : prevLower;
    }
    let dir = prevDir;
    if (i > period) {
      if (prevST === prevUpper) dir = b.c > upper ? 1 : -1;
      else                      dir = b.c < lower ? -1 : 1;
    } else {
      dir = b.c >= mid ? 1 : -1;
    }
    const st = dir === 1 ? lower : upper;
    out.push({ time: b.ts, value: st, dir });
    prevUpper = upper; prevLower = lower; prevST = st; prevDir = dir;
  }
  return out;
}

// Heikin-Ashi transform. HA close = avg(o,h,l,c); HA open = avg(prev HA o,c).
function toHeikin(bars) {
  const out = [];
  let prevO = null, prevC = null;
  for (const b of bars) {
    const haC = (b.o + b.h + b.l + b.c) / 4;
    const haO = prevO == null ? (b.o + b.c) / 2 : (prevO + prevC) / 2;
    const haH = Math.max(b.h, haO, haC);
    const haL = Math.min(b.l, haO, haC);
    out.push({ time: b.ts, open: haO, high: haH, low: haL, close: haC });
    prevO = haO; prevC = haC;
  }
  return out;
}

// ── Drawing layer: horizontal lines (S/R, manual SL/TP), alerts, measure ─────

const DRAW_LS = (symbol) => `fb_draw_${symbol}`;
const loadLines = (symbol) => {
  try { return JSON.parse(localStorage.getItem(DRAW_LS(symbol))) || []; }
  catch { return []; }
};
const saveLines = (symbol, lines) => {
  try { localStorage.setItem(DRAW_LS(symbol), JSON.stringify(lines)); } catch {}
};

const LINE_COLORS = {
  ray:   "#5c9eff",   // manual S/R line
  alert: "#ffb300",   // price alert
};

// Render horizontal lines + alerts + in-progress measure onto the draw canvas.
function drawOverlay(canvas, chart, candleSeries, lines, measure, hoverId) {
  if (!canvas || !chart || !candleSeries) return;
  const rect = canvas.getBoundingClientRect();
  const W = Math.round(rect.width)  || canvas.parentElement?.clientWidth  || 800;
  const H = Math.round(rect.height) || canvas.parentElement?.clientHeight || 400;
  if (canvas.width !== W)  canvas.width  = W;
  if (canvas.height !== H) canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, W, H);

  ctx.font = "10px JetBrains Mono, monospace";
  for (const ln of lines || []) {
    const y = candleSeries.priceToCoordinate(ln.price);
    if (y === null) continue;
    const col = ln.triggered ? "#ef5350" : (LINE_COLORS[ln.kind] || "#888");
    const isAlert = ln.kind === "alert";
    ctx.strokeStyle = col;
    ctx.lineWidth = ln.id === hoverId ? 2 : 1;
    ctx.setLineDash(isAlert ? [6, 4] : []);
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.setLineDash([]);
    // label chip (left)
    const px = ln.price >= 100 ? ln.price.toFixed(1) : ln.price.toFixed(2);
    const glyph = isAlert ? (ln.triggered ? "🔔!" : "🔔") : "—";
    const label = `${glyph} ${px}${ln.note ? " " + ln.note : ""}`;
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = "rgba(13,13,13,0.85)";
    ctx.fillRect(2, y - 7, tw + 8, 13);
    ctx.fillStyle = col;
    ctx.textBaseline = "middle"; ctx.textAlign = "left";
    ctx.fillText(label, 6, y);
    // delete hint dot (right) when hovered
    if (ln.id === hoverId) {
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(W - 8, y, 3, 0, 2 * Math.PI); ctx.fill();
    }
  }

  if (measure && measure.p1 != null && measure.p2 != null) {
    const y1 = candleSeries.priceToCoordinate(measure.p1);
    const y2 = candleSeries.priceToCoordinate(measure.p2);
    const x1 = measure.x1, x2 = measure.x2;
    if (y1 != null && y2 != null) {
      const up = measure.p2 >= measure.p1;
      const col = up ? "rgba(38,166,154,1)" : "rgba(239,83,80,1)";
      const fill = up ? "rgba(38,166,154,0.12)" : "rgba(239,83,80,0.12)";
      ctx.fillStyle = fill;
      ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
      ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
      ctx.setLineDash([]);
      // readout box
      const dPrice = measure.p2 - measure.p1;
      const pct = measure.p1 ? (dPrice / measure.p1) * 100 : 0;
      const nbars = measure.bars != null ? measure.bars : "";
      const lines2 = [
        `${dPrice >= 0 ? "+" : ""}${dPrice >= 100 || dPrice <= -100 ? dPrice.toFixed(1) : dPrice.toFixed(2)}  (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`,
        nbars !== "" ? `${nbars} bars` : "",
      ].filter(Boolean);
      const bw = Math.max(...lines2.map(s => ctx.measureText(s).width)) + 12;
      const bx = Math.min(x1, x2) + Math.abs(x2 - x1) / 2 - bw / 2;
      const by = Math.min(y1, y2) - (lines2.length * 14) - 6;
      ctx.fillStyle = "rgba(13,13,13,0.9)";
      ctx.fillRect(bx, by, bw, lines2.length * 14 + 4);
      ctx.strokeStyle = col; ctx.strokeRect(bx, by, bw, lines2.length * 14 + 4);
      ctx.fillStyle = "#ddd"; ctx.textAlign = "left"; ctx.textBaseline = "top";
      lines2.forEach((s, i) => ctx.fillText(s, bx + 6, by + 4 + i * 14));
    }
  }
}

// Resolve a trade's real outcome from future candles. Used for trades that
// carry no concrete closed result (open trades, or records missing r/exit) —
// walk bars after entry and report whichever of TP / SL price touches first.
// Trades that already closed with a managed exit (trail / flip / invalidation)
// keep their stored result, since that exit isn't a plain TP/SL touch.
function resolveTradeOutcome(t, bars) {
  const hasClosedResult = !t.open && (t.r != null || t.exit_price != null);
  if (hasClosedResult) return t;
  if (t.entry == null || t.entry_ts == null || t.sl == null || t.tp == null) return t;
  const isLong = t.side === "long";
  for (const b of bars) {
    if (b.ts <= t.entry_ts) continue;
    const hitTP = isLong ? b.h >= t.tp : b.l <= t.tp;
    const hitSL = isLong ? b.l <= t.sl : b.h >= t.sl;
    if (!hitTP && !hitSL) continue;
    // Both touched in the same bar → can't tell order; assume SL first (honest).
    const tpFirst = hitTP && !hitSL;
    const both = hitTP && hitSL;
    const risk = Math.abs(t.entry - t.sl) || 1e-9;
    return {
      ...t, open: false, _resolved: true,
      exit_ts: b.ts,
      exit_price: tpFirst ? t.tp : t.sl,
      r: tpFirst ? Math.abs(t.tp - t.entry) / risk : -1,
      reason: tpFirst ? "TP hit (candle)" : both ? "SL/TP same bar→SL" : "SL hit (candle)",
    };
  }
  return t;  // still running — no touch yet
}

export default function CandleChart({ bars: rawBars, dailyVp, priorVp, detections, positions, nakedPocs, historicalSweeps, swingPoints, strategyTrades = [], cvd = [], cvdDivergences = [], symbol, chartMode, indicators, chartType = "candles", logScale = false }) {
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
  const tpCanvasRef  = useRef(null);   // trade position boxes
  // Enrich trades with candle-derived outcomes (open / unresolved trades get
  // their real TP-or-SL-first result from future bars).
  const resolvedTrades = useMemo(
    () => (strategyTrades || []).map(t => resolveTradeOutcome(t, bars)),
    [strategyTrades, bars],
  );
  const stratTradesRef = useRef(resolvedTrades);
  stratTradesRef.current = resolvedTrades;
  const redrawRef    = useRef(null);   // exposes redrawAll to other effects
  const [showBubbles, setShowBubbles] = useState(() => {
    try { return localStorage.getItem("fb_show_bubbles") !== "0"; } catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem("fb_show_bubbles", showBubbles ? "1" : "0"); } catch {}
  }, [showBubbles]);
  const bubbleHitsRef = useRef([]);
  const [hoverBubble, setHoverBubble] = useState(null);  // {bt, x, y}
  const cdCanvasRef  = useRef(null);   // CVD divergence overlay
  const cvdDivRef    = useRef(cvdDivergences);
  cvdDivRef.current  = cvdDivergences;
  const divHitsRef   = useRef([]);
  const [hoverDiv, setHoverDiv] = useState(null);  // {d, x, y}
  const showCvdDivRef = useRef(!!indicators?.cvdDiv);
  useEffect(() => { showCvdDivRef.current = !!indicators?.cvdDiv; }, [indicators]);
  const bigTradesRef = useRef([]);
  const showBubblesRef = useRef(showBubbles);
  useEffect(() => { bigTradesRef.current = detections?.big_trades || []; }, [detections]);
  useEffect(() => { showBubblesRef.current = showBubbles; }, [showBubbles]);
  const chartRef     = useRef(null);
  const candleRef    = useRef(null);
  const deltaRef     = useRef(null);
  const cvdRef       = useRef(null);
  const lineRef      = useRef(null);   // close-line series (line chartType)
  const vwapRef      = useRef(null);
  const vwapBandRef  = useRef([]);     // [u1,l1,u2,l2] line series
  const stRef        = useRef(null);   // SuperTrend trail series
  const [countdown, setCountdown] = useState("");
  const [legend, setLegend]       = useState(null);  // {o,h,l,c,delta,vol,up}
  // Drawing layer (measure / horizontal ray / alert)
  const drawCanvasRef = useRef(null);
  const [drawTool, setDrawTool] = useState(null);    // null|'measure'|'ray'|'alert'
  const [lines, setLines]       = useState(() => loadLines(symbol));
  const [hoverLineId, setHoverLineId] = useState(null);
  const drawLinesRef = useRef(lines);   drawLinesRef.current = lines;
  const drawToolRef = useRef(drawTool); drawToolRef.current = drawTool;
  const hoverLineRef = useRef(null);
  const measureRef  = useRef(null);
  const dragRef     = useRef(null);
  const alertPrevClose = useRef(null);
  let _lineSeq = useRef(1);
  const linesRef       = useRef([]);
  const pivotSeriesRef = useRef([]);
  const markersRef   = useRef([]);
  const [hoverMarker, setHoverMarker] = useState(null);
  const barsRef      = useRef(bars);
  const dailyVpRef   = useRef(dailyVp);
  const prevSymbol   = useRef(symbol);
  const fitBarsRef   = useRef(null);   // bars ref at last symbol-fit (race guard)
  barsRef.current    = bars;
  dailyVpRef.current = dailyVp;
  const vpModeRef    = useRef("visible");
  vpModeRef.current  = indicators?.vpFull ? "full" : "visible";

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

    // Close-line series for the "line" chart type (hidden unless selected).
    const lineSeries = chart.addLineSeries({
      color: "#d0d0d0", lineWidth: 2, priceLineVisible: false,
      lastValueVisible: false, visible: false,
    });

    // VWAP + volume-weighted σ bands (price-scale series → autoscale w/ candles).
    const vwapSeries = chart.addLineSeries({
      color: "#ff9800", lineWidth: 2, priceLineVisible: false,
      lastValueVisible: true, title: "VWAP", visible: false,
    });
    const mkBand = (color) => chart.addLineSeries({
      color, lineWidth: 1, lineStyle: LineStyle.Dotted,
      priceLineVisible: false, lastValueVisible: false,
      crosshairMarkerVisible: false, visible: false,
    });
    const bandSeries = [
      mkBand("rgba(255,152,0,0.45)"),  // u1
      mkBand("rgba(255,152,0,0.45)"),  // l1
      mkBand("rgba(255,152,0,0.22)"),  // u2
      mkBand("rgba(255,152,0,0.22)"),  // l2
    ];

    // SuperTrend trail — ATR-based visual TSL (color set per-point via markers
    // not supported on a single line; we recolor by recreating segments in the
    // data effect). Single series, neutral base color.
    const stSeries = chart.addLineSeries({
      color: "#26a69a", lineWidth: 2, lineStyle: LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, visible: false,
    });

    chartRef.current  = chart;
    candleRef.current = candleSeries;
    deltaRef.current  = deltaSeries;
    cvdRef.current    = cvdSeries;
    lineRef.current   = lineSeries;
    vwapRef.current   = vwapSeries;
    vwapBandRef.current = bandSeries;
    stRef.current     = stSeries;

    const redrawAll = () => {
      drawVP(vpCanvasRef.current, chart, candleSeries, dailyVpRef.current, barsRef.current, vpModeRef.current);
      drawFootprint(fpCanvasRef.current, chart, candleSeries, barsRef.current);
      drawBubbles(bbCanvasRef.current, chart, candleSeries, bigTradesRef.current, showBubblesRef.current, barsRef.current, bubbleHitsRef.current);
      drawTradeBoxes(tpCanvasRef.current, chart, candleSeries, stratTradesRef.current, barsRef.current);
      drawCVDDivergence(cdCanvasRef.current, chart, candleSeries, cvdDivRef.current, barsRef.current, showCvdDivRef.current, divHitsRef.current);
      drawOverlay(drawCanvasRef.current, chart, candleSeries, drawLinesRef.current, measureRef.current, hoverLineRef.current);
    };
    redrawRef.current = redrawAll;
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
      // Hit-test CVD divergence markers.
      let dHit = null;
      for (const h of divHitsRef.current) {
        if (Math.hypot(mx - h.x, my - h.y) <= h.r + 2) { dHit = h; break; }
      }
      setHoverDiv(prev => {
        if (!dHit && !prev) return prev;
        if (dHit && prev && prev.d === dHit.d) return { ...prev, x: mx, y: my };
        return dHit ? { d: dHit.d, x: mx, y: my } : null;
      });
    };
    const onMouseLeave = () => { setHoverBubble(null); setHoverDiv(null); };
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
      for (const c of [vpCanvasRef.current, fpCanvasRef.current, bbCanvasRef.current, tpCanvasRef.current, cdCanvasRef.current, drawCanvasRef.current]) {
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

    // Candle body: raw OHLC or Heikin-Ashi transform. "line" type feeds a
    // separate close-line series (candle hidden); visibility handled in the
    // chartType effect below.
    const candleData = chartType === "heikin"
      ? toHeikin(bars)
      : bars.map(b => ({ time: b.ts, open: b.o, high: b.h, low: b.l, close: b.c }));
    candleRef.current.setData(candleData);
    lineRef.current?.setData(bars.map(b => ({ time: b.ts, value: b.c })));

    // VWAP + σ bands
    const vw = computeVWAP(bars);
    vwapRef.current?.setData(vw.vwap);
    const bandSeries = vwapBandRef.current;
    if (bandSeries?.length === 4) {
      bandSeries[0].setData(vw.u1); bandSeries[1].setData(vw.l1);
      bandSeries[2].setData(vw.u2); bandSeries[3].setData(vw.l2);
    }

    // SuperTrend trail — recolor by mapping each point's dir to a per-point
    // color (lightweight-charts line series supports per-point `color`).
    const st = computeSuperTrend(bars);
    stRef.current?.setData(st.map(p => ({
      time: p.time, value: p.value,
      color: p.dir === 1 ? COLORS.up : COLORS.down,
    })));

    deltaRef.current.setData(bars.map(b => ({
      time: b.ts, value: b.delta,
      color: b.delta >= 0 ? COLORS.up : COLORS.down,
    })));

    // CVD line: prefer backend session-reset CVD candles (consistent with the
    // divergence markers); fall back to local cumulative delta.
    if (cvd?.length) {
      const seen = new Set();
      const cvdData = [];
      for (const c of cvd) {
        if (seen.has(c.ts)) continue;
        seen.add(c.ts);
        cvdData.push({ time: c.ts, value: c.c });
      }
      cvdData.sort((a, b) => a.time - b.time);
      cvdRef.current.setData(cvdData);
    } else {
      let cvdSum = 0;
      cvdRef.current.setData(bars.map(b => {
        cvdSum += b.delta || 0;
        return { time: b.ts, value: cvdSum };
      }));
    }
    const showCvd = !!indicators?.cvdDiv;
    cvdRef.current.applyOptions({ visible: showCvd });
    chartRef.current?.priceScale("cvd").applyOptions({ visible: showCvd });

    // Fit + rescale ONLY on the render where the new symbol's data actually
    // arrives. On symbol switch the effect first runs with the *previous*
    // symbol's bars still in place (data refetch is async); fitting/marking
    // prevSymbol there would consume the fit on stale data and leave the view
    // stuck on the old instrument when the new bars land. Gate on bars-ref
    // change so we fit when XAUT/BTC data truly swaps in (not on every tick —
    // prevSymbol===symbol then).
    if (prevSymbol.current !== symbol && bars !== fitBarsRef.current) {
      prevSymbol.current = symbol;
      chartRef.current?.timeScale().fitContent();
      candleRef.current?.priceScale().applyOptions({ autoScale: true });
    }
    fitBarsRef.current = bars;

    drawVP(vpCanvasRef.current, chartRef.current, candleRef.current, dailyVp, bars, vpModeRef.current);
    // Defer bubble draw a frame so chart's internal coordinate mapping is
    // up-to-date with the just-applied setData call.
    requestAnimationFrame(() => {
      drawBubbles(bbCanvasRef.current, chartRef.current, candleRef.current,
                  detections?.big_trades || [], showBubbles, bars, bubbleHitsRef.current);
      drawCVDDivergence(cdCanvasRef.current, chartRef.current, candleRef.current,
                        cvdDivergences, bars, !!indicators?.cvdDiv, divHitsRef.current);
    });

    if (chartMode === "footprint") {
      drawFootprintWithRetry(fpCanvasRef.current, chartRef.current, candleRef.current, bars);
    }
  }, [bars, symbol, detections, showBubbles, chartType, cvd, cvdDivergences, indicators]);

  // Chart-type visibility: candle body vs close-line.
  useEffect(() => {
    if (!candleRef.current) return;
    const isLine = chartType === "line";
    try {
      candleRef.current.applyOptions({ visible: !isLine });
      lineRef.current?.applyOptions({ visible: isLine });
    } catch {}
  }, [chartType]);

  // Log / linear price scale.
  useEffect(() => {
    try {
      candleRef.current?.priceScale().applyOptions({
        mode: logScale ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal,
      });
    } catch {}
  }, [logScale]);

  // VWAP / SuperTrend visibility from the layers toggle.
  useEffect(() => {
    const showVwap = indicators?.vwap === true;
    const showSt   = indicators?.atrTrail === true;
    try {
      vwapRef.current?.applyOptions({ visible: showVwap });
      vwapBandRef.current?.forEach(s => s.applyOptions({ visible: showVwap }));
      stRef.current?.applyOptions({ visible: showSt });
    } catch {}
    redrawRef.current?.();   // vpFull mode change → repaint VP
  }, [indicators]);

  // Bar-close countdown — last bar's ts IS its close timestamp; tick every 1s.
  useEffect(() => {
    if (!bars.length) { setCountdown(""); return; }
    const closeTs = bars[bars.length - 1].ts;
    const tick = () => {
      const left = Math.round(closeTs - Date.now() / 1000);
      if (left < 0 || left > 86400) { setCountdown(""); return; }
      const m = Math.floor(left / 60), s = left % 60;
      setCountdown(`${m}:${String(s).padStart(2, "0")}`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [bars]);

  // Crosshair OHLC/Δ legend readout (TV-style top-left live values).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const handler = (param) => {
      if (!param.time) { setLegend(null); return; }
      const b = barsRef.current.find(x => x.ts === param.time);
      if (!b) { setLegend(null); return; }
      setLegend({
        o: b.o, h: b.h, l: b.l, c: b.c, delta: b.delta,
        vol: (b.bid_vol || 0) + (b.ask_vol || 0), up: b.c >= b.o,
      });
    };
    chart.subscribeCrosshairMove(handler);
    return () => { try { chart.unsubscribeCrosshairMove(handler); } catch {} };
  }, []);

  // Reload drawn lines when symbol changes (lines are price-level, symbol-scoped).
  useEffect(() => { setLines(loadLines(symbol)); measureRef.current = null; }, [symbol]);

  // Persist lines when the set changes.
  useEffect(() => { saveLines(symbol, lines); }, [lines, symbol]);
  // Repaint on line-set or hover change (cheap, no storage write).
  useEffect(() => { redrawRef.current?.(); }, [lines, hoverLineId]);

  // Clear measure + hover when leaving a tool.
  useEffect(() => {
    if (!drawTool) { measureRef.current = null; dragRef.current = null; redrawRef.current?.(); }
  }, [drawTool]);

  // Alert firing — detect close crossing an armed alert line, notify once.
  useEffect(() => {
    if (!bars.length) return;
    const close = bars[bars.length - 1].c;
    const prev = alertPrevClose.current;
    alertPrevClose.current = close;
    if (prev == null) return;
    let changed = false;
    const next = lines.map(ln => {
      if (ln.kind !== "alert" || ln.triggered) return ln;
      if ((prev - ln.price) * (close - ln.price) <= 0) {
        changed = true;
        try {
          if (typeof Notification !== "undefined" && Notification.permission === "granted") {
            new Notification(`${symbol} alert`, { body: `price crossed ${ln.price}` });
          }
        } catch {}
        return { ...ln, triggered: true };
      }
      return ln;
    });
    if (changed) setLines(next);
  }, [bars, symbol]);

  // Interaction handlers on the draw canvas (active only when a tool is on).
  useEffect(() => {
    const cv = drawCanvasRef.current;
    if (!cv) return;
    const priceAt = (y) => { try { return candleRef.current?.coordinateToPrice(y); } catch { return null; } };
    const timeAt  = (x) => { try { return chartRef.current?.timeScale().coordinateToTime(x); } catch { return null; } };
    const nearestLine = (y) => {
      const cs = candleRef.current; if (!cs) return null;
      for (const ln of drawLinesRef.current) {
        const ly = cs.priceToCoordinate(ln.price);
        if (ly != null && Math.abs(ly - y) <= 6) return ln;
      }
      return null;
    };
    const barsBetween = (t1, t2) => {
      if (t1 == null || t2 == null) return 0;
      const lo = Math.min(t1, t2), hi = Math.max(t1, t2);
      return barsRef.current.filter(b => b.ts >= lo && b.ts <= hi).length;
    };
    const xy = (e) => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };

    const onDown = (e) => {
      const tool = drawToolRef.current; if (!tool) return;
      const [mx, my] = xy(e);
      const p = priceAt(my); if (p == null) return;
      if (tool === "measure") {
        dragRef.current = { mode: "measure" };
        measureRef.current = { x1: mx, y1: my, p1: p, t1: timeAt(mx), x2: mx, y2: my, p2: p, bars: 0 };
      } else {
        const hit = nearestLine(my);
        if (hit) { dragRef.current = { mode: "line", id: hit.id }; }
        else {
          const id = `l${Date.now()}_${_lineSeq.current++}`;
          if (tool === "alert" && typeof Notification !== "undefined" && Notification.permission === "default") {
            try { Notification.requestPermission(); } catch {}
          }
          setLines(prev => [...prev, { id, price: p, kind: tool, note: "", triggered: false }]);
        }
      }
      redrawRef.current?.();
    };
    const onMove = (e) => {
      const [mx, my] = xy(e);
      const drag = dragRef.current;
      if (drag?.mode === "measure" && measureRef.current) {
        const p = priceAt(my);
        measureRef.current = { ...measureRef.current, x2: mx, y2: my, p2: p ?? measureRef.current.p2, bars: barsBetween(measureRef.current.t1, timeAt(mx)) };
        redrawRef.current?.();
        return;
      }
      if (drag?.mode === "line") {
        const p = priceAt(my); if (p == null) return;
        const arr = drawLinesRef.current.map(l => l.id === drag.id ? { ...l, price: p, triggered: false } : l);
        drawLinesRef.current = arr;        // live drag without state churn
        redrawRef.current?.();
        return;
      }
      // hover highlight
      if (drawToolRef.current) {
        const hit = nearestLine(my);
        hoverLineRef.current = hit?.id ?? null;
        setHoverLineId(prev => prev === (hit?.id ?? null) ? prev : (hit?.id ?? null));
      }
    };
    const onUp = () => {
      const drag = dragRef.current;
      if (drag?.mode === "line") setLines([...drawLinesRef.current]);  // commit + persist
      dragRef.current = null;
    };
    const onDbl = (e) => {
      if (!drawToolRef.current) return;
      const [, my] = xy(e);
      const hit = nearestLine(my);
      if (hit) setLines(prev => prev.filter(l => l.id !== hit.id));
    };

    cv.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    cv.addEventListener("dblclick", onDbl);
    return () => {
      cv.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      cv.removeEventListener("dblclick", onDbl);
    };
  }, []);

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
    drawVP(vpCanvasRef.current, chartRef.current, candleRef.current, dailyVp, bars, vpModeRef.current);
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

    // Coup absorption trigger candles (momentum mode) — full window
    if (indicators?.coupAbs !== false) {
      (detections?.coup_absorptions || []).forEach(a => {
        const isLong = a.winner === "long";
        markers.push({
          time: a.ts,
          position: isLong ? "belowBar" : "aboveBar",
          color: isLong ? COLORS.up : COLORS.down,
          shape: isLong ? "arrowUp" : "arrowDown",
          text: `cABS`,
          size: 1,
          absObs: { ...a, strat: "coup", modeLabel: "momentum" },
        });
      });
    }

    // Coup-reversal absorption trigger candles (reversal mode) — full window
    if (indicators?.coupRevAbs !== false) {
      (detections?.coup_reversal_absorptions || []).forEach(a => {
        const isLong = a.winner === "long";
        markers.push({
          time: a.ts,
          position: isLong ? "belowBar" : "aboveBar",
          color: isLong ? "#26c6da" : "#ab47bc",
          shape: isLong ? "arrowUp" : "arrowDown",
          text: `crABS`,
          size: 1,
          absObs: { ...a, strat: "coup_reversal", modeLabel: "reversal" },
        });
      });
    }

    // Confirmed absorptions ★ — only candles whose next bar confirmed the reversal
    // (the high-continuation subset). Gold squares, both modes.
    if (indicators?.coupAbsConf) {
      const conf = [
        ...(detections?.coup_absorptions || []).map(a => ({ ...a, strat: "coup", modeLabel: "momentum" })),
        ...(detections?.coup_reversal_absorptions || []).map(a => ({ ...a, strat: "coup_reversal", modeLabel: "reversal" })),
      ].filter(a => a.next_confirms === true);
      conf.forEach(a => {
        const isLong = a.winner === "long";
        markers.push({
          time: a.ts,
          position: isLong ? "belowBar" : "aboveBar",
          color: "#ffd700",
          shape: "square",
          text: `★ ${a.continuation ? "CONT" : ""}`.trim(),
          size: 2,
          absObs: a,
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

    // ── Strategy / grid trades ───────────────────────────────────────────────
    // Entry/exit ARROWS here (markers, hover-enabled); the entry/SL/TP zones are
    // drawn as gradient position boxes on the trade canvas (drawTradeBoxes).
    // Strategy trades (coup/democracy/republic/m1/m2; backtest + live). Entry arrow
    // colored per strategy; entry/SL/TP time-bounded segments. Open trades have
    // no exit_ts → segments extend to the current bar (so a TP-less open trade
    // shows entry→now + its SL). Entry reason shown on hover via marker.reason.
    resolvedTrades.forEach(t => {
      const isLong = t.side === "long";
      const strat = t.strategy || "coup";
      const col = STRAT_COLORS[strat] || COLORS.up;
      const isOpen = t.open || !t.exit_ts;
      const tag = STRAT_TAG[strat] || strat[0].toUpperCase();
      markers.push({
        time: t.entry_ts, position: isLong ? "belowBar" : "aboveBar",
        color: col, shape: isLong ? "arrowUp" : "arrowDown",
        text: `${tag}${isLong ? "↑" : "↓"}`, size: 2,
        strategy: strat, side: t.side, isOpen,
        reason: t.rationale || t.entry_mode || "",
        entry: t.entry, sl: t.sl, tp: t.tp,
        confidence: t.confidence, bias: t.bias_strength,
        invalidation: t.invalidation_note,
        openTime: t.entry_ts_ist, exitTime: t.exit_ts_ist,
        r: t.r, exitReason: t.reason,
        isCycle: t.is_cycle, cycleNum: t.cycle_num, pnl: t.pnl,
      });
      if (t.exit_ts) {
        const rTxt = t.r == null ? "" : `${t.r >= 0 ? "+" : ""}${t.r.toFixed(2)}R`;
        const win = (t.r ?? 0) > 0;
        markers.push({
          time: t.exit_ts, position: isLong ? "aboveBar" : "belowBar",
          color: win ? COLORS.up : COLORS.down, shape: "circle",
          text: `${t.reason || "exit"} ${rTxt}`.trim(), size: 1,
        });
      }
      // entry/SL/TP drawn as gradient position boxes on the trade canvas
      // (drawTradeBoxes), not as line series — better UX, less clutter.
    });

    markers.sort((a, b) => a.time - b.time);
    if (markers.length) try { cs.setMarkers(markers); } catch {}
    markersRef.current = markers;
    // repaint trade boxes whenever the trade set changes
    redrawRef.current?.();

  }, [dailyVp, priorVp, detections, positions, nakedPocs, historicalSweeps, swingPoints, resolvedTrades, bars, indicators]);

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
      {/* Trade position boxes — under VP/bubbles so they read as background */}
      <canvas ref={tpCanvasRef} style={{ ...canvasStyle, zIndex: 4 }} />
      {/* VP overlay — always shown */}
      <canvas ref={vpCanvasRef} style={{ ...canvasStyle, zIndex: 5 }} />
      {/* Bubble overlay — always rendered; visibility gated inside drawBubbles */}
      <canvas ref={bbCanvasRef} style={{ ...canvasStyle, zIndex: 6 }} />
      {/* CVD divergence overlay — visibility gated inside drawCVDDivergence */}
      <canvas ref={cdCanvasRef} style={{ ...canvasStyle, zIndex: 7 }} />
      {/* Footprint overlay — FP mode only */}
      <canvas
        ref={fpCanvasRef}
        style={{ ...canvasStyle, display: chartMode === "footprint" ? "block" : "none" }}
      />
      {/* Drawing layer — captures mouse only when a tool is active */}
      <canvas
        ref={drawCanvasRef}
        style={{
          ...canvasStyle, zIndex: 8,
          pointerEvents: drawTool ? "auto" : "none",
          cursor: drawTool === "measure" ? "crosshair" : drawTool ? "ns-resize" : "default",
        }}
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
      {/* CVD divergence hover tooltip */}
      {hoverDiv && (() => {
        const d = hoverDiv.d;
        const bear = d.type === "bear";
        const color = bear ? COLORS.down : COLORS.up;
        const W = 220, H = 86;
        const wrapRect = containerRef.current?.getBoundingClientRect() || { width: 800, height: 400 };
        let tx = hoverDiv.x + 12, ty = hoverDiv.y + 12;
        if (tx + W > wrapRect.width)  tx = hoverDiv.x - W - 12;
        if (ty + H > wrapRect.height) ty = hoverDiv.y - H - 12;
        const tIst = d.ts ? (() => {
          const dt = new Date((d.ts + 19800) * 1000);
          return `${String(dt.getUTCHours()).padStart(2,"0")}:${String(dt.getUTCMinutes()).padStart(2,"0")} IST`;
        })() : "";
        return (
          <div style={{
            position: "absolute", left: tx, top: ty,
            background: "rgba(13,13,13,0.92)", border: `1px solid ${color}`,
            borderRadius: 3, padding: "6px 8px", pointerEvents: "none",
            zIndex: 20, minWidth: W, fontFamily: "JetBrains Mono, monospace",
            fontSize: 11, color: "#bbb", lineHeight: 1.5,
          }}>
            <div style={{ color, fontWeight: 700 }}>
              CVD DIVERGENCE · {bear ? "BEARISH" : "BULLISH"}
            </div>
            <div>{bear ? "price higher-high, CVD lower-high" : "price lower-low, CVD higher-low"}</div>
            <div><span style={{ color: "#888" }}>price </span><span>{d.price}</span>
              <span style={{ color: "#888" }}>  str </span><span>{d.strength}</span></div>
            <div><span style={{ color: "#888" }}>cvd </span><span>{d.cvd}</span>
              <span style={{ color: "#888" }}> vs </span><span>{d.prev_cvd}</span></div>
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

        // Absorption candle — rich card: who trapped, aggressor, next-candle confirm.
        if (m.absObs) {
          const a = m.absObs;
          const isLong = a.winner === "long";
          const nc = a.next_confirms;
          return (
            <div style={{
              position: "absolute", left: tx, top: ty,
              background: "rgba(13,13,13,0.94)", border: `1px solid ${m.color}`,
              borderRadius: 3, padding: "6px 8px", pointerEvents: "none",
              zIndex: 20, minWidth: 230, fontFamily: "JetBrains Mono, monospace",
              fontSize: 11, color: "#bbb", lineHeight: 1.5,
            }}>
              <div style={{ color: m.color, fontWeight: 700 }}>
                {a.strat.toUpperCase()} · {a.winner.toUpperCase()} ({a.modeLabel})
              </div>
              <div>{a.bar_dir} candle · vol×{a.vol_ratio} · Δ{a.delta}</div>
              <div><span style={{ color: isLong ? COLORS.up : COLORS.down }}>
                {a.trapped} trapped → {a.winner}</span></div>
              <div>extreme aggressor <b>{a.zone_aggressor}</b>
                {a.zone_buy_frac != null ? ` (buy ${(a.zone_buy_frac * 100).toFixed(0)}%)` : ""}
                {"  "}ask {a.zone_ask} / bid {a.zone_bid}</div>
              <div>absPct {a.bar_pct}{a.is_wick_trap ? " · wick-trap" : ""}</div>
              {nc != null && (
                <div style={{ color: nc ? COLORS.up : "#888", marginTop: 1 }}>
                  next candle {nc ? "✔ CONFIRMS" : "✗ no confirm"}
                  {a.next_delta != null ? ` (Δ${a.next_delta})` : ""}
                </div>
              )}
              {a.fwd_mfe_atr != null && (
                <div style={{ color: a.continuation ? COLORS.up : COLORS.down, marginTop: 1 }}>
                  fwd 6-bar: MFE {a.fwd_mfe_atr}atr / MAE {a.fwd_mae_atr}atr
                  {"  "}[{a.continuation ? "CONT" : "fail"}]
                </div>
              )}
            </div>
          );
        }

        // Strategy trade entry — rich card: strategy, side, entry/SL/TP, reason.
        if (m.strategy) {
          const fmt = (v) => (v == null ? "—" : (+v).toFixed(2));
          const sideTxt = (m.side || "").toUpperCase();
          const rr = (m.entry != null && m.sl != null && m.tp != null && m.entry !== m.sl)
            ? `  RR ${(Math.abs(m.tp - m.entry) / Math.abs(m.entry - m.sl)).toFixed(2)}` : "";
          return (
            <div style={{
              position: "absolute", left: tx, top: ty,
              background: "rgba(13,13,13,0.94)", border: `1px solid ${m.color}`,
              borderRadius: 3, padding: "6px 8px", pointerEvents: "none",
              zIndex: 20, minWidth: 220, fontFamily: "JetBrains Mono, monospace",
              fontSize: 11, color: "#bbb", lineHeight: 1.5,
            }}>
              <div style={{ color: m.color, fontWeight: 700 }}>
                {m.strategy.toUpperCase()} {sideTxt}
                {m.isCycle ? ` · cycle${m.cycleNum != null ? ` #${m.cycleNum}` : ""}` : ""}
                {m.bias ? ` · bias ${m.bias}/5` : ""}
                <span style={{ color: m.isOpen ? "#ffd700" : "#888", marginLeft: 6 }}>
                  {m.isOpen ? "● OPEN" : "○ closed"}
                </span>
              </div>
              {m.isCycle && m.pnl != null && (
                <div style={{ color: m.pnl >= 0 ? COLORS.up : COLORS.down }}>
                  PnL {m.pnl >= 0 ? "+" : ""}{(+m.pnl).toFixed(2)}
                </div>
              )}
              {m.openTime && <div style={{ color: "#999" }}>opened {m.openTime}</div>}
              <div>entry {fmt(m.entry)}{m.confidence != null ? `  conf ${(+m.confidence).toFixed(2)}` : ""}</div>
              <div><span style={{ color: COLORS.sl }}>SL {fmt(m.sl)}</span>{"   "}
                <span style={{ color: COLORS.tp }}>{m.tp != null ? `TP ${fmt(m.tp)}` : "TP — (open)"}</span>{rr}</div>
              {!m.isOpen && (m.exitReason || m.r != null) && (
                <div style={{ color: "#999", marginTop: 1 }}>
                  exit {m.r != null ? `${m.r >= 0 ? "+" : ""}${(+m.r).toFixed(2)}R` : ""}
                  {m.exitTime ? `  ${m.exitTime}` : ""}
                </div>
              )}
              {m.reason && <div style={{ color: "#888", marginTop: 2 }}>↳ {m.reason}</div>}
              {!m.isOpen && m.exitReason && <div style={{ color: "#777" }}>⇲ {m.exitReason}</div>}
              {m.invalidation && <div style={{ color: "#777", marginTop: 1 }}>✕ {m.invalidation}</div>}
            </div>
          );
        }

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
        {/* Drawing tools — measure / horizontal ray / price alert */}
        <div style={{ display: "flex", gap: 4 }}>
          {[
            { k: "measure", lbl: "📏", tip: "Measure: drag to read Δprice / % / bars" },
            { k: "ray",     lbl: "─",  tip: "Horizontal line (manual S/R, SL/TP). Click to place, drag to move, dbl-click to delete" },
            { k: "alert",   lbl: "🔔", tip: "Price alert: click to place; notifies when price crosses. Drag to move, dbl-click to delete" },
          ].map(t => {
            const on = drawTool === t.k;
            return (
              <button
                key={t.k}
                onClick={() => setDrawTool(cur => cur === t.k ? null : t.k)}
                title={t.tip}
                style={{
                  background: "rgba(26,26,26,0.85)",
                  border: `1px solid ${on ? "#5c9eff" : "#2a2a2a"}`,
                  color: on ? "#5c9eff" : "#888",
                  padding: "2px 6px", cursor: "pointer", borderRadius: 3, lineHeight: 1.4,
                }}
              >
                {t.lbl}
              </button>
            );
          })}
          {lines.length > 0 && (
            <button
              onClick={() => setLines([])}
              title="Clear all drawn lines & alerts for this symbol"
              style={{
                background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
                color: "#888", padding: "2px 6px", cursor: "pointer", borderRadius: 3, lineHeight: 1.4,
              }}
            >
              ✕{lines.length}
            </button>
          )}
        </div>
        {/* OHLC / Δ legend — follows crosshair (TV top-left readout) */}
        {legend && (
          <div style={{
            background: "rgba(13,13,13,0.85)", border: "1px solid #2a2a2a",
            borderRadius: 3, padding: "2px 7px", color: "#999", lineHeight: 1.5,
            whiteSpace: "nowrap",
          }}>
            <span style={{ color: legend.up ? COLORS.up : COLORS.down }}>
              O{legend.o} H{legend.h} L{legend.l} C{legend.c}
            </span>
            <span style={{ color: legend.delta >= 0 ? COLORS.up : COLORS.down, marginLeft: 6 }}>
              Δ{legend.delta >= 0 ? "+" : ""}{Math.abs(legend.delta) >= 100 ? legend.delta.toFixed(0) : legend.delta.toFixed(1)}
            </span>
            <span style={{ color: "#666", marginLeft: 6 }}>V{legend.vol >= 1000 ? (legend.vol / 1000).toFixed(1) + "k" : legend.vol.toFixed(0)}</span>
          </div>
        )}
      </div>
      {/* Bar-close countdown — top-center badge */}
      {countdown && (
        <div style={{
          position: "absolute", top: 6, left: "50%", transform: "translateX(-50%)",
          zIndex: 10, background: "rgba(26,26,26,0.85)", border: "1px solid #2a2a2a",
          borderRadius: 3, padding: "2px 8px", fontFamily: "JetBrains Mono, monospace",
          fontSize: 11, color: "#888", lineHeight: 1.4,
        }}>
          ⏱ {countdown}
        </div>
      )}
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
