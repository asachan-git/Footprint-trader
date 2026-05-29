import { useState, useEffect, useCallback, useRef } from "react";
import { fetchState } from "./api.js";
import CandleChart from "./components/CandleChart.jsx";
import FootprintPane from "./components/FootprintPane.jsx";
import VoteBreakdown from "./components/VoteBreakdown.jsx";
import DecisionCards from "./components/DecisionCards.jsx";
import Positions from "./components/Positions.jsx";
import ABStats from "./components/ABStats.jsx";

const SYMBOLS  = ["BTCUSDT", "XAUTUSDT"];
const TFS      = ["1m", "5m", "15m"];
const WINDOWS  = [60, 120, 240, 480, 1440];
const POLL_MS  = 10_000;

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
  const [data,      setData]      = useState(null);
  const [stale,     setStale]     = useState(false);
  const [lastTs,    setLastTs]    = useState(null);
  const timerRef = useRef(null);

  // resizable heights (px)
  const [chartH,  setChartH]  = useState(() => Math.round(window.innerHeight * 0.58));
  const [bottomH, setBottomH] = useState(200);

  const refresh = useCallback(async () => {
    try {
      const result = await fetchState(symbol, tf, minutes, chartMode === "footprint");
      setData(result);
      setStale(false);
      setLastTs(new Date().toLocaleTimeString());
    } catch (e) {
      console.error("fetchState failed:", e);
      setStale(true);
    }
  }, [symbol, tf, minutes, chartMode]);

  useEffect(() => {
    refresh();
    timerRef.current = setInterval(refresh, POLL_MS);
    return () => clearInterval(timerRef.current);
  }, [refresh]);

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
            {w < 60 ? `${w}m` : w === 1440 ? "1D" : `${w}m`}
          </button>
        ))}
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

      {/* chart pane */}
      <div style={{ height: chartH, flexShrink: 0, overflow: "hidden" }}>
        {chartMode === "footprint" ? (
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
            symbol={symbol}
          />
        ) : (
          <CandleChart
            bars={data?.bars ?? []}
            dailyVp={data?.daily_vp ?? {}}
            priorVp={data?.prior_vp ?? {}}
            detections={data?.detections ?? {}}
            positions={data?.positions?.open ?? []}
            symbol={symbol}
            chartMode={chartMode}
          />
        )}
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
