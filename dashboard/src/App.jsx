import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import CandleChart from "./components/CandleChart.jsx";
import FootprintPane from "./components/FootprintPane.jsx";
import VoteBreakdown from "./components/VoteBreakdown.jsx";
import DecisionCards from "./components/DecisionCards.jsx";
import Positions from "./components/Positions.jsx";
import ABStats from "./components/ABStats.jsx";
import { fetchStrategyTrades, fetchGridTrades, fetchStrategyCycles, fetchGridCycles, fetchStrategyOutcomes, fetchCvdSweeps, fetchStrategies } from "./api.js";

// Strategy-trade layers handled explicitly (own toggles + colors). Every OTHER
// live strategy is auto-listed in the dynamic "Strategy Trades" layers section.
const HARDCODED_STRATS = ["coup", "democracy", "republic"];

// Outcome-range presets → seconds back from now (null secs = all-time).
const OUTCOME_RANGES = [
  { key: "24h", label: "24h",    secs: 86400 },
  { key: "7d",  label: "7d",     secs: 7 * 86400 },
  { key: "30d", label: "30d",    secs: 30 * 86400 },
  { key: "all", label: "All",    secs: null },
  { key: "custom", label: "Custom", secs: undefined },
];

const SYMBOLS  = ["BTCUSDT", "XAUTUSDT"];
const TFS      = ["1m", "5m", "15m"];
const WINDOWS  = [60, 120, 240, 480, 1440, 7200];
const STREAM_INTERVAL = 1.5;

const LS_IND_KEY  = "fb_indicators_v1";
const DEFAULT_IND = {
  sweepArrows: true,  eqLevels: true,   pivotLines: false, nakedPocs: true,  priorPoc: true,
  liveSweep:   true,  srFsSweeps: false, fvgLines:  false,  priorVa:   false, absorptions: false,
  coupAbs:     false, coupRevAbs: false, coupAbsConf: false,
  coupTrades:  true,  demoTrades: true, repTrades: true,
  m1Trades:    false, m2Trades: false, cycleTrades: false,
  vwap:        false, atrTrail: false, bollinger: false, vpFull: false, vpDaily: false, sessionLines: true, cvdDiv: false, cvdEqDiv: false, cvdLine: false, revPattern: false,
  chochSetup:  false, waveSetup: false,
  cvdSweeps:   false, cvdSweepsDivOnly: false,
};

const IND_ITEMS = [
  { key: "sweepArrows",  label: "Sweep Arrows (REV/LG)", tip: "Reversal & Liquidity Grab — delta-confirmed sweeps of key levels; high institutional signal" },
  { key: "srFsSweeps",   label: "SR / FS Sweeps",        tip: "Stop Run & Failed Sweep — lower-confidence sweeps without clear delta confirmation" },
  { key: "liveSweep",    label: "Live Sweep Level",       tip: "Current bar wick extreme — live price level being swept right now" },
  { key: "eqLevels",     label: "EQH / EQL",             tip: "Equal Highs/Lows — two+ pivots within 0.15% form a retail stop cluster; prime institutional sweep target" },
  { key: "pivotLines",   label: "Pivot Lines",            tip: "Structural swing highs/lows — local price extremes (TF-aware lookback). Line ends when level is taken out" },
  { key: "nakedPocs",    label: "Naked POCs",             tip: "Naked Point of Control — prior-session POCs price has never returned to test; strong magnet levels" },
  { key: "priorPoc",     label: "Prior POC",              tip: "Prior session Point of Control — high-volume node from yesterday's session" },
  { key: "priorVa",      label: "Prior VA (H/L)",         tip: "Prior session Value Area High/Low — 70% of prior-day volume traded between these two levels" },
  { key: "fvgLines",     label: "FVGs",                   tip: "Fair Value Gaps — 3-bar imbalance zones where price moved too fast; often filled on retest" },
  { key: "absorptions",  label: "Absorptions",            tip: "Absorption — large passive volume at price extreme with minimal bar movement; potential reversal signal" },
  { key: "coupAbs",      label: "Coup Absorptions",       tip: "coup trigger candles (momentum mode): high-vol/high-delta 15m bar with the winner side dominant at the extreme. ▲ below = long, ▼ above = short" },
  { key: "coupRevAbs",   label: "Coup-Reversal Absorptions", tip: "coup_reversal trigger candles (reversal mode): high-vol candle closing with a wick rejection + volume fight at the extreme. ◆ marker, color = winner side" },
  { key: "coupAbsConf",  label: "Confirmed Absorptions ★", tip: "ONLY absorption candles whose NEXT candle confirmed the reversal (winner-dir delta dominant + closed past trigger). Gold squares — the high-continuation subset. Both coup + coup_reversal" },
  { key: "coupTrades",   label: "Coup Trades",            tip: "coup strategy entries/exits (15m, orange). Backtest + live: entry/exit arrows, SL/TP segments" },
  { key: "demoTrades",   label: "Democracy Trades",       tip: "democracy strategy live paper trades (15m, blue). Entry arrow + SL/TP segments; hover for reason" },
  { key: "repTrades",    label: "Republic Trades",        tip: "republic strategy live paper trades (15m, purple, tight-SL). Entry arrow + SL/TP segments; hover for reason" },
  { key: "m1Trades",     label: "M1 Trades (history)",    tip: "Mode-1 Claude-direction grid trade history (15m, teal). Open/closed entries + SL/TP; hover for open time, reason, R" },
  { key: "m2Trades",     label: "M2 Trades (history)",    tip: "Mode-2 rules grid trade history (15m, amber). Open/closed entries + SL/TP; hover for open time, reason, R" },
  { key: "cycleTrades",  label: "Cycle Trades",           tip: "Grid recovery cycles (dashed boxes) for the enabled strategies/modes. Shows cycle # + chain; hover for realized PnL + reason" },
  { key: "vwap",         label: "VWAP + σ bands",         tip: "Session-anchored VWAP (UTC day) with volume-weighted ±1σ/±2σ bands. Dynamic SL/TP anchor; mean-revert target = VWAP, stretch target = ±2σ" },
  { key: "atrTrail",     label: "ATR Trail (SuperTrend)", tip: "ATR-based SuperTrend trailing stop. Sits below price in uptrend (green), above in downtrend (red). Visual TSL / trend filter" },
  { key: "bollinger",    label: "Bollinger Bands",        tip: "20-period SMA ±2σ envelope of close (per the selected TF). Basis = mean-revert target; price riding the upper/lower band = stretch. Cyan bands + center line" },
  { key: "vpFull",       label: "VP: full window",        tip: "Volume profile (left edge) scope: ON = whole loaded window, OFF = visible range only. Real bid/ask volume-at-price with POC + 70% value area + HVN/LVN" },
  { key: "vpDaily",      label: "VP: per-day",            tip: "Session-anchored volume profile PER DAY (XAU 03:30 IST / BTC 05:30 IST), anchored to each day's candle span (market-profile style). Faint per-day POC/VAH/VAL + HVN/LVN tint + day separators. Independent of the left-edge VP" },
  { key: "sessionLines", label: "Session lines",          tip: "Vertical dashed lines at each session boundary (XAU 03:30 IST / BTC 05:30 IST) — marks the start of each day's session (= prior session's end), labeled with the date. Drawn on both OHLC and Footprint panes" },
  { key: "cvdDiv",       label: "CVD Divergence (HH/LL)",  tip: "STRICT regular divergence at swing-HL pivots + current candle (no in-between). Bear ▽ (red) at higher-high w/ lower CVD; bull △ (green) at lower-low w/ higher CVD. Hollow = live (current bar, provisional). Toggle 'CVD Div (EqH/L)' for equal-high/low divergences separately" },
  { key: "cvdEqDiv",     label: "CVD Div (EqH/L)",        tip: "EQUAL-high/low divergence: price retests the same extreme (±0.15%) but CVD makes a lower/higher value = distribution at the liquidity level. Same ▽/△ marker + a small '=' tick. Independent of the strict HH/LL toggle" },
  { key: "cvdLine",      label: "CVD Line",               tip: "Continuous Cumulative Volume Delta line (secondary scale), every bar. Separate from the divergence markers so the markers can show clean without the line clutter" },
  { key: "revPattern",   label: "Reversal Pattern ⚑",     tip: "DIAGNOSTIC (not a trade signal — backtests ~breakeven). Climax pivot (vol≥2×med) + next-bar delta flip (closes reversal way + rev-delta swing ≥50). Rendered as TRADES (⚑, cyan): entry→exit box w/ structural SL (swing high/low + buffer) + 2R TP, win/loss colored, outcome resolved from bars; hover for reason/RR. Any TF" },
  { key: "chochSetup",   label: "ChoCh→Fib ⤰",            tip: "reversal_choch setups (15m only). After a Change of Character (break of the last HL/LH) it arms a counter-trend reversal: LIMIT at the 0.705 fib retrace of the impulse leg, SL beyond the swing origin, TP at the 2.0 extension. Rendered as a trade box (entry→SL/TP, outcome resolved from bars); hover for the ChoCh dir + leg" },
  { key: "waveSetup",    label: "Wave-Fib (3rd wave) ⤴", tip: "wave_fib continuation setups (15m only). After a two-wave trend confirms (HH+HL / LL+LH) it arms a WITH-trend 3rd-wave entry: LIMIT at the VP-value edge of the two-wave structure, SL beyond the HL/LH, TP at 2.0× the wave-1 measured move. Rendered as a trade box; hover for the wave structure" },
  { key: "cvdSweeps",    label: "CVD Sweep Trades ⚐",    tip: "Simulated trades from scripts/cvd_sweep_study.py (5m/15m only). HVN-extreme sweep + close-reclaim setups, with T1/T2/T3 horizontal lines (T1 opp HVN edge, T2 next HVN, T3 LVN extreme), SL = sweep wick. Entry triangle colored by outcome (T2 cyan, T3 indigo, T1 green, SL red, TIME gray). Solid marker = CVD div intact (thesis subset); faint = no div." },
  { key: "cvdSweepsDivOnly", label: "CVD Sweep — div-intact only", tip: "Filter the CVD Sweep Trades layer to ONLY setups with CVD divergence intact (the thesis subset). Cuts the visual noise from the 80%+ no-div population. No effect unless 'CVD Sweep Trades' is also on." },
];

function LayersToggle({ indicators, onChange, dynamicStrats = [] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{ borderColor: open ? "var(--cyan)" : undefined, color: open ? "var(--cyan)" : undefined }}
      >
        {open ? "▾" : "▸"} LAYERS
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "calc(100% + 4px)", left: 0,
          background: "#111", border: "1px solid #2a2a2a", borderRadius: 4,
          padding: "4px 0", zIndex: 200, minWidth: 150,
          boxShadow: "0 4px 12px rgba(0,0,0,0.7)",
          fontFamily: "JetBrains Mono, monospace", fontSize: 11,
          overflow: 'scroll', maxHeight: 600
        }}>
          {IND_ITEMS.map(item => {
            const on = indicators[item.key] !== false;
            return (
              <div
                key={item.key}
                onClick={() => onChange(item.key, !on)}
                title={item.tip}
                style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 12px",
                  cursor: "pointer", color: on ? "#ccc" : "#444", userSelect: "none" }}
                onMouseEnter={e => e.currentTarget.style.background = "#1a1a1a"}
                onMouseLeave={e => e.currentTarget.style.background = ""}
              >
                <span style={{ color: on ? "#00bcd4" : "#333", fontSize: 13 }}>{on ? "◉" : "○"}</span>
                {item.label}
              </div>
            );
          })}
          {dynamicStrats.length > 0 && (
            <div style={{ padding: "6px 12px 2px", color: "#666", fontSize: 9,
              letterSpacing: 1, borderTop: "1px solid #2a2a2a", marginTop: 4 }}>
              STRATEGY TRADES
            </div>
          )}
          {dynamicStrats.map(name => {
            const key = `strat:${name}`;
            const on = indicators[key] === true;     // default OFF (avoid flooding)
            return (
              <div
                key={key}
                onClick={() => onChange(key, !on)}
                title={`${name} live/backtest trades on both panes`}
                style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 12px",
                  cursor: "pointer", color: on ? "#ccc" : "#444", userSelect: "none" }}
                onMouseEnter={e => e.currentTarget.style.background = "#1a1a1a"}
                onMouseLeave={e => e.currentTarget.style.background = ""}
              >
                <span style={{ color: on ? "#00bcd4" : "#333", fontSize: 13 }}>{on ? "◉" : "○"}</span>
                {name}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── simple drag-resize divider ────────────────────────────────────────────────
function DragHandle({ direction = "row", onDelta }) {
  const dragging = useRef(false);
  const last = useRef(0);

  const onMouseDown = (e) => {
    dragging.current = true;
    last.current = direction === "row" ? e.clientY : e.clientX;
    e.preventDefault();
  };

  useEffect(() => {
    const onMove = (e) => {
      if (!dragging.current) return;
      const pos = direction === "row" ? e.clientY : e.clientX;
      onDelta(pos - last.current);
      last.current = pos;
    };
    const onUp = () => { dragging.current = false; };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [direction, onDelta]);

  const style = direction === "row"
    ? { height: 4, cursor: "row-resize", background: "var(--border)", flexShrink: 0, userSelect: "none" }
    : { width: 4, cursor: "col-resize", background: "var(--border)", flexShrink: 0, userSelect: "none" };

  return <div style={style} onMouseDown={onMouseDown} />;
}

// ── app ───────────────────────────────────────────────────────────────────────
const LS_KEY = "fb_dashboard_v1";
const loadPrefs = () => {
  try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; }
  catch { return {}; }
};
const _prefs = loadPrefs();

export default function App() {
  const [symbol,    setSymbol]    = useState(_prefs.symbol    || "BTCUSDT");
  const [tf,        setTf]        = useState(_prefs.tf        || "1m");
  const [minutes,   setMinutes]   = useState(_prefs.minutes   || 120);
  const [chartMode, setChartMode] = useState(_prefs.chartMode || "ohlc");
  const [chartType, setChartType] = useState(_prefs.chartType || "candles"); // candles|heikin|line
  const [logScale,  setLogScale]  = useState(_prefs.logScale  || false);

  // Persist toolbar selections across reloads
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({ symbol, tf, minutes, chartMode, chartType, logScale }));
    } catch {}
  }, [symbol, tf, minutes, chartMode, chartType, logScale]);
  const [indicators, setIndicators] = useState(() => {
    try { return { ...DEFAULT_IND, ...JSON.parse(localStorage.getItem(LS_IND_KEY)) }; }
    catch { return DEFAULT_IND; }
  });
  useEffect(() => {
    try { localStorage.setItem(LS_IND_KEY, JSON.stringify(indicators)); } catch {}
  }, [indicators]);
  const toggleIndicator = useCallback((key, val) => setIndicators(p => ({ ...p, [key]: val })), []);

  const [data,      setData]      = useState(null);
  const [stale,     setStale]     = useState(false);
  const [lastTs,    setLastTs]    = useState(null);
  const [stratTrades, setStratTrades] = useState({ coup: [], democracy: [], republic: [], m1: [], m2: [] });
  const [stratCycles, setStratCycles] = useState({ coup: [], democracy: [], republic: [], m1: [] });
  const [cvdSweeps,   setCvdSweeps]   = useState([]);
  const [extraStrats, setExtraStrats] = useState([]);   // live strategy names minus HARDCODED_STRATS
  const [extraTrades, setExtraTrades] = useState({});   // name → trades[] (dynamic Strategy-Trades layers)
  const esRef = useRef(null);

  // Per-strategy outcomes panel — range selector (preset or custom from/to).
  const [outcomeRange, setOutcomeRange] = useState("24h");
  const [outcomeFrom, setOutcomeFrom] = useState("");  // yyyy-mm-dd (custom)
  const [outcomeTo, setOutcomeTo]     = useState("");
  const [outcomeRows, setOutcomeRows] = useState([]);
  useEffect(() => {
    let cancel = false;
    const load = async () => {
      let from = null, to = null;
      if (outcomeRange === "custom") {
        if (!outcomeFrom) return;                       // wait for a start date
        from = Math.floor(new Date(outcomeFrom + "T00:00:00").getTime() / 1000);
        if (outcomeTo) to = Math.floor(new Date(outcomeTo + "T23:59:59").getTime() / 1000);
      } else {
        const r = OUTCOME_RANGES.find(x => x.key === outcomeRange);
        from = r && r.secs != null ? Math.floor(Date.now() / 1000) - r.secs : 0;
      }
      try {
        const rows = await fetchStrategyOutcomes(from, to);
        if (!cancel) setOutcomeRows(rows);
      } catch { /* keep last */ }
    };
    load();
    const id = setInterval(load, 30000);
    return () => { cancel = true; clearInterval(id); };
  }, [outcomeRange, outcomeFrom, outcomeTo]);

  // Strategy + grid-mode trades + cycles for the chart overlay. coup =
  // backtest+live, democracy/republic = live paper, m1/m2 = grid-mode history.
  // Backend filters by symbol/tf; all entries are 15m. Refreshed on symbol/tf
  // change + every 30s so new live entries appear without a reload.
  useEffect(() => {
    let cancel = false;
    const load = () => {
      Promise.all([
        fetchStrategyTrades("coup", symbol, tf, "all").catch(() => []),
        fetchStrategyTrades("democracy", symbol, tf, "live").catch(() => []),
        fetchStrategyTrades("republic", symbol, tf, "live").catch(() => []),
        fetchGridTrades("m1", symbol, tf).catch(() => []),
        fetchGridTrades("m2", symbol, tf).catch(() => []),
        fetchStrategyCycles("coup", symbol, tf).catch(() => []),
        fetchStrategyCycles("democracy", symbol, tf).catch(() => []),
        fetchStrategyCycles("republic", symbol, tf).catch(() => []),
        fetchGridCycles("m1", symbol, tf).catch(() => []),
      ]).then(([coup, democracy, republic, m1, m2, cCoup, cDemo, cRep, cM1]) => {
        if (cancel) return;
        setStratTrades({ coup, democracy, republic, m1, m2 });
        setStratCycles({ coup: cCoup, democracy: cDemo, republic: cRep, m1: cM1 });
      });
    };
    load();
    const id = setInterval(load, 30000);
    return () => { cancel = true; clearInterval(id); };
  }, [symbol, tf]);

  // Dynamic Strategy-Trades layers: fetch the live strategy list, then trades for
  // every strategy NOT hardcoded above (congress, senate, reversal_si, choch/wave
  // variants, …). Feeds the same `strategyTrades` array → both candle + FP panes.
  useEffect(() => {
    let cancel = false;
    const load = async () => {
      let names = [];
      try { names = await fetchStrategies(); } catch { return; }
      const extra = names.filter(n => !HARDCODED_STRATS.includes(n));
      if (cancel) return;
      setExtraStrats(extra);
      const lists = await Promise.all(extra.map(n =>
        fetchStrategyTrades(n, symbol, tf, "all").catch(() => [])));
      if (cancel) return;
      setExtraTrades(Object.fromEntries(extra.map((n, i) => [n, lists[i]])));
    };
    load();
    const id = setInterval(load, 30000);
    return () => { cancel = true; clearInterval(id); };
  }, [symbol, tf]);

  // CVD sweep study setups — backtest output, only available for 5m/15m JSONLs.
  useEffect(() => {
    if (tf !== "5m" && tf !== "15m") { setCvdSweeps([]); return; }
    let cancel = false;
    fetchCvdSweeps(symbol, tf).then(rows => { if (!cancel) setCvdSweeps(rows); })
                              .catch(() => { if (!cancel) setCvdSweeps([]); });
    return () => { cancel = true; };
  }, [symbol, tf]);

  // Filter to thesis subset if the sub-toggle is on (else keep the whole list).
  const cvdSweepsView = useMemo(() => {
    if (!indicators.cvdSweeps) return [];
    if (indicators.cvdSweepsDivOnly) return cvdSweeps.filter(s => s.cvd_div_intact);
    return cvdSweeps;
  }, [cvdSweeps, indicators.cvdSweeps, indicators.cvdSweepsDivOnly]);

  // Merge enabled strategies/modes into one tagged array for the chart.
  const reversalPatterns = data?.reversal_patterns ?? [];
  const chochSetups = data?.choch_setups ?? [];
  const waveSetups  = data?.wave_setups ?? [];
  const strategyTrades = useMemo(() => {
    const tag = (arr, name) => (arr || []).map(t => ({ ...t, strategy: name }));
    const out = [];
    // Reversal-pattern "trades" (diagnostic) — any TF; outcome resolved from bars.
    if (indicators.revPattern) {
      out.push(...(reversalPatterns || []).map(p => ({
        strategy: "reversal", side: p.side,
        entry: p.entry, entry_ts: p.ts, sl: p.sl, tp: p.tp, open: true,
        rationale: `reversal: climax vol×${p.vol_ratio} + flip Δswing${p.delta_swing} · SL=${p.sl_basis}`,
      })));
    }
    // ChoCh→Fib + Wave-Fib setups (15m only; armed at arm_ts, outcome from bars).
    if (indicators.chochSetup) {
      out.push(...(chochSetups || []).map(p => ({
        strategy: "reversal_choch", side: p.side,
        entry: p.entry, entry_ts: p.arm_ts, sl: p.sl, tp: p.tp, open: true,
        rationale: `choch ${p.choch_dir}: fib ${p.fib_entry}/${p.fib_ext} · leg ${p.origin}→${p.extreme} · broke ${p.broken_level}`,
      })));
    }
    if (indicators.waveSetup) {
      out.push(...(waveSetups || []).map(p => ({
        strategy: "wave_fib", side: p.side,
        entry: p.entry, entry_ts: p.arm_ts, sl: p.sl, tp: p.tp, open: true,
        rationale: `wave_fib ${p.entry_kind}: wave-1 ${p.origin}→${p.impulse} · pivot ${p.pullback} · ${p.fib_ext}× MM`,
      })));
    }
    // Dynamic Strategy-Trades layers — any tf (each strategy's trades come back
    // only for its own decide_tf, so 15m strats render on 15m, _5m on 5m). Default
    // OFF (=== true) so the chart isn't flooded; toggled in the LAYERS dropdown.
    for (const n of extraStrats) {
      if (indicators[`strat:${n}`] === true) out.push(...tag(extraTrades[n], n));
    }
    if (tf !== "15m") return out;
    if (indicators.coupTrades !== false) out.push(...tag(stratTrades.coup, "coup"));
    if (indicators.demoTrades !== false) out.push(...tag(stratTrades.democracy, "democracy"));
    if (indicators.repTrades  !== false) out.push(...tag(stratTrades.republic, "republic"));
    if (indicators.m1Trades) out.push(...tag(stratTrades.m1, "m1"));
    if (indicators.m2Trades) out.push(...tag(stratTrades.m2, "m2"));
    if (indicators.cycleTrades) {
      if (indicators.coupTrades !== false) out.push(...tag(stratCycles.coup, "coup"));
      if (indicators.demoTrades !== false) out.push(...tag(stratCycles.democracy, "democracy"));
      if (indicators.repTrades  !== false) out.push(...tag(stratCycles.republic, "republic"));
      if (indicators.m1Trades) out.push(...tag(stratCycles.m1, "m1"));
    }
    return out;
  }, [stratTrades, stratCycles, indicators.coupTrades, indicators.demoTrades,
      indicators.repTrades, indicators.m1Trades, indicators.m2Trades, indicators.cycleTrades,
      indicators.revPattern, reversalPatterns,
      indicators.chochSetup, chochSetups, indicators.waveSetup, waveSetups, tf,
      extraStrats, extraTrades, indicators]);
  // Shared visible-time-range across FP ↔ Candle. Each pane writes on pan,
  // reads on becoming visible. Ref, not state, so no re-render loops.
  const rangeRef = useRef(null);  // {from: ts, to: ts} | null
  const writeRange = (from, to) => { rangeRef.current = { from, to }; };
  const readRange  = () => rangeRef.current;

  // resizable heights (px)
  const [chartH,  setChartH]  = useState(() => Math.round(window.innerHeight * 0.58));
  const [bottomH, setBottomH] = useState(200);

  // SSE — server pushes snapshots whenever state changes; lightweight "tick"
  // events keep live bar fresh between snapshot pushes.
  // chartMode NOT in deps: switching FP↔OHLC must not reconnect the stream
  // (would push a fresh snapshot → chart resets view). Always request ladders
  // so the FP pane has data ready when toggled.
  useEffect(() => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    const params = new URLSearchParams({
      symbol, tf, minutes: String(minutes),
      interval: String(STREAM_INTERVAL),
      footprint: "true",
    });
    if (minutes === 1440) params.set("session", "today");
    const es = new EventSource(`/dashboard/stream?${params}`);
    esRef.current = es;
    es.addEventListener("snapshot", (ev) => {
      try {
        setData(JSON.parse(ev.data));
        setStale(false);
        setLastTs(new Date().toLocaleTimeString());
      } catch (e) { console.error("snapshot parse failed:", e); }
    });
    es.addEventListener("tick", (ev) => {
      try {
        const t = JSON.parse(ev.data);
        if (!t?.live_bar) return;
        setData(prev => {
          if (!prev?.bars?.length) return prev;
          const bars = [...prev.bars];
          const last = bars[bars.length - 1];
          // Replace last bar only if same ts (live-bar update); else append.
          if (last && last.ts === t.live_bar.ts) {
            bars[bars.length - 1] = t.live_bar;
          } else if (t.live_bar.ts > (last?.ts ?? 0)) {
            bars.push(t.live_bar);
          }
          return { ...prev, bars };
        });
        setLastTs(new Date().toLocaleTimeString());
      } catch (e) { console.error("tick parse failed:", e); }
    });
    es.onerror = () => { setStale(true); };
    return () => { es.close(); esRef.current = null; };
  }, [symbol, tf, minutes]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", overflow: "hidden" }}>
      {/* toolbar */}
      <div className="toolbar">
        <span style={{ color: "var(--text-dim)", marginRight: 4 }}>FOOTPRINT</span>
        {SYMBOLS.map(s => (
          <button
            key={s}
            onClick={() => setSymbol(s)}
            style={{ borderColor: s === symbol ? "var(--cyan)" : undefined,
                     color:       s === symbol ? "var(--cyan)" : undefined }}
          >
            {s}
          </button>
        ))}
        <span style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
        {TFS.map(t => (
          <button
            key={t}
            onClick={() => setTf(t)}
            style={{ borderColor: t === tf ? "var(--yellow)" : undefined,
                     color:       t === tf ? "var(--yellow)" : undefined }}
          >
            {t}
          </button>
        ))}
        <span style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
        {WINDOWS.map(w => (
          <button
            key={w}
            onClick={() => setMinutes(w)}
            style={{ borderColor: w === minutes ? "var(--text-dim)" : undefined }}
          >
            {w < 60 ? `${w}m` : w === 1440 ? "1D" : w === 7200 ? "5D" : `${w}m`}
          </button>
        ))}
        <span style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
        <LayersToggle indicators={indicators} onChange={toggleIndicator} dynamicStrats={extraStrats} />
        <span style={{ width: 1, height: 20, background: "var(--border)", margin: "0 4px" }} />
        <button
          onClick={() => setChartMode(m => m === "ohlc" ? "footprint" : "ohlc")}
          style={{
            borderColor: chartMode === "footprint" ? "var(--orange)" : undefined,
            color:       chartMode === "footprint" ? "var(--orange)" : undefined,
          }}
        >
          {chartMode === "ohlc" ? "OHLC" : "FP"}
        </button>
        {chartMode === "ohlc" && (
          <>
            <button
              onClick={() => setChartType(t => t === "candles" ? "heikin" : t === "heikin" ? "line" : "candles")}
              title="Cycle chart type: candles → Heikin-Ashi → line"
            >
              {chartType === "candles" ? "▦ CANDLE" : chartType === "heikin" ? "▨ HA" : "╱ LINE"}
            </button>
            <button
              onClick={() => setLogScale(v => !v)}
              title="Toggle log / linear price scale"
              style={{ borderColor: logScale ? "var(--yellow)" : undefined,
                       color:       logScale ? "var(--yellow)" : undefined }}
            >
              {logScale ? "LOG" : "LIN"}
            </button>
          </>
        )}
        <div className={`live-dot${stale ? " stale" : ""}`} />
        <span className="last-update">{lastTs ? `updated ${lastTs}` : "loading…"}</span>
      </div>

      {/* chart pane — both mounted always at full size; visibility toggled so
          their internal dims stay valid and view state persists across mode switch */}
      <div style={{ height: chartH, flexShrink: 0, overflow: "hidden", position: "relative" }}>
        <div style={{
          position: "absolute", inset: 0,
          visibility: chartMode === "footprint" ? "visible" : "hidden",
          pointerEvents: chartMode === "footprint" ? "auto" : "none",
          zIndex: chartMode === "footprint" ? 2 : 1,
        }}>
          <FootprintPane
            bars={data?.bars ?? []}
            dailyVp={data?.daily_vp ?? {}}
            priorVp={data?.prior_vp ?? {}}
            detections={data?.detections ?? {}}
            positions={data?.positions?.open ?? []}
            pendingOrders={data?.positions?.pending ?? []}
            closedPositions={data?.positions?.closed ?? []}
            cvd={data?.cvd ?? []}
            cvdSignal={data?.cvd_signal ?? null}
            cvdDivergences={data?.cvd_divergences ?? []}
            zones={data?.zones ?? {}}
            latestM2={data?.latest_m2 ?? null}
            nakedPocs={data?.naked_pocs ?? []}
            historicalSweeps={data?.historical_sweeps ?? []}
            swingPoints={data?.swing_points ?? []}
            strategyTrades={strategyTrades}
            symbol={symbol}
            indicators={indicators}
          />
        </div>
        <div style={{
          position: "absolute", inset: 0,
          visibility: chartMode === "ohlc" ? "visible" : "hidden",
          pointerEvents: chartMode === "ohlc" ? "auto" : "none",
          zIndex: chartMode === "ohlc" ? 2 : 1,
        }}>
          <CandleChart
            bars={data?.bars ?? []}
            dailyVp={data?.daily_vp ?? {}}
            priorVp={data?.prior_vp ?? {}}
            detections={data?.detections ?? {}}
            positions={data?.positions?.open ?? []}
            nakedPocs={data?.naked_pocs ?? []}
            historicalSweeps={data?.historical_sweeps ?? []}
            swingPoints={data?.swing_points ?? []}
            strategyTrades={strategyTrades}
            cvd={data?.cvd ?? []}
            cvdDivergences={data?.cvd_divergences ?? []}
            cvdSweeps={cvdSweepsView}
            tf={tf}
            symbol={symbol}
            chartMode={chartMode}
            indicators={indicators}
            chartType={chartType}
            logScale={logScale}
          />
        </div>
      </div>

      <DragHandle direction="row" onDelta={(d) => setChartH(h => Math.max(150, h + d))} />

      {/* bottom row: votes | cards | positions */}
      <div style={{ height: bottomH, flexShrink: 0, display: "flex", overflow: "hidden", borderTop: "1px solid var(--border)" }}>
        <div style={{ width: 280, flexShrink: 0, borderRight: "1px solid var(--border)", overflow: "auto", padding: 8 }}>
          <div className="panel-title">M2 Votes</div>
          <VoteBreakdown votes={data?.latest_m2?.votes ?? []} />
        </div>
        <DragHandle direction="col" onDelta={() => {}} />
        <div style={{ flex: 1, overflow: "hidden" }}>
          <DecisionCards m1={data?.latest_m1} m2={data?.latest_m2} />
        </div>
        <DragHandle direction="col" onDelta={() => {}} />
        <div style={{ width: 280, flexShrink: 0, borderLeft: "1px solid var(--border)", overflow: "auto", padding: 8 }}>
          <div className="panel-title">Open Positions</div>
          <Positions positions={data?.positions?.open ?? []} />
        </div>
      </div>

      <DragHandle direction="row" onDelta={(d) => setBottomH(h => Math.max(80, h + d))} />

      {/* stats row */}
      <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden", borderTop: "1px solid var(--border)" }}>
        <div style={{ flex: 1, overflow: "auto", padding: 8, borderRight: "1px solid var(--border)" }}>
          <div className="panel-title">A/B Comparison</div>
          <ABStats abStats={data?.ab_stats} />
        </div>
        <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
          <div className="panel-title" style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <span>Strategy Outcomes</span>
            <select value={outcomeRange} onChange={e => setOutcomeRange(e.target.value)}
                    style={{ fontSize: 11, padding: "1px 4px" }}>
              {OUTCOME_RANGES.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
            </select>
            {outcomeRange === "custom" && (
              <>
                <input type="date" value={outcomeFrom} onChange={e => setOutcomeFrom(e.target.value)}
                       style={{ fontSize: 11 }} />
                <span style={{ color: "#888" }}>→</span>
                <input type="date" value={outcomeTo} onChange={e => setOutcomeTo(e.target.value)}
                       style={{ fontSize: 11 }} />
              </>
            )}
          </div>
          <ABStats abStats={data?.ab_stats} outcomesOnly strategyRows={outcomeRows} />
        </div>
      </div>
    </div>
  );
}
