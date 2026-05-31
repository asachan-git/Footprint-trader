import { useState, useEffect, useRef, useCallback } from "react";
import CandleChart from "./components/CandleChart.jsx";
import FootprintPane from "./components/FootprintPane.jsx";
import VoteBreakdown from "./components/VoteBreakdown.jsx";
import DecisionCards from "./components/DecisionCards.jsx";
import Positions from "./components/Positions.jsx";
import ABStats from "./components/ABStats.jsx";

const SYMBOLS  = ["BTCUSDT", "XAUTUSDT"];
const TFS      = ["1m", "5m", "15m"];
const WINDOWS  = [60, 120, 240, 480, 1440, 7200];
const STREAM_INTERVAL = 1.5;

const LS_IND_KEY  = "fb_indicators_v1";
const DEFAULT_IND = {
  sweepArrows: true,  eqLevels: true,   pivotLines: false, nakedPocs: true,  priorPoc: true,
  liveSweep:   true,  srFsSweeps: false, fvgLines:  false,  priorVa:   false, absorptions: false,
};

const IND_ITEMS = [
  { key: "sweepArrows",  label: "Sweep Arrows (REV/LG)" },
  { key: "srFsSweeps",   label: "SR / FS Sweeps" },
  { key: "liveSweep",    label: "Live Sweep Level" },
  { key: "eqLevels",     label: "EQH / EQL" },
  { key: "pivotLines",   label: "Pivot Lines" },
  { key: "nakedPocs",    label: "Naked POCs" },
  { key: "priorPoc",     label: "Prior POC" },
  { key: "priorVa",      label: "Prior VA (H/L)" },
  { key: "fvgLines",     label: "FVGs" },
  { key: "absorptions",  label: "Absorptions" },
];

function LayersToggle({ indicators, onChange }) {
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
        }}>
          {IND_ITEMS.map(item => {
            const on = indicators[item.key] !== false;
            return (
              <div
                key={item.key}
                onClick={() => onChange(item.key, !on)}
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

  // Persist toolbar selections across reloads
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({ symbol, tf, minutes, chartMode }));
    } catch {}
  }, [symbol, tf, minutes, chartMode]);
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
  const esRef = useRef(null);
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
        <LayersToggle indicators={indicators} onChange={toggleIndicator} />
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
            zones={data?.zones ?? {}}
            latestM2={data?.latest_m2 ?? null}
            nakedPocs={data?.naked_pocs ?? []}
            historicalSweeps={data?.historical_sweeps ?? []}
            swingPoints={data?.swing_points ?? []}
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
            symbol={symbol}
            chartMode={chartMode}
            indicators={indicators}
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
          <div className="panel-title">24h Outcomes</div>
          <ABStats abStats={data?.ab_stats} outcomesOnly />
        </div>
      </div>
    </div>
  );
}
