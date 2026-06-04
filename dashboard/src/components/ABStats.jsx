const SYMBOLS = ["BTCUSDT", "XAUTUSDT"];

function rClass(v) {
  if (v > 0) return "pos";
  if (v < 0) return "neg";
  return "";
}

export default function ABStats({ abStats, outcomesOnly = false, strategyRows = null }) {
  if (!abStats && !outcomesOnly) return <div className="empty">No data</div>;

  if (outcomesOnly) {
    const o = (abStats && abStats.outcomes_24h) || {};
    const strat = strategyRows ?? (abStats && abStats.strategies_24h) ?? [];
    return (
      <>
        <div className="outcome-chips">
          <div className="chip">
            <div className={`chip-val pos`}>{o.tp_hit ?? 0}</div>
            <div className="chip-lbl">TP Hit</div>
          </div>
          <div className="chip">
            <div className={`chip-val neg`}>{o.sl_hit ?? 0}</div>
            <div className="chip-lbl">SL Hit</div>
          </div>
          <div className="chip">
            <div className="chip-val">{o.invalidated ?? 0}</div>
            <div className="chip-lbl">Invalidated</div>
          </div>
          <div className="chip">
            <div className={`chip-val ${rClass(o.sum_r)}`}>
              {(o.sum_r ?? 0).toFixed(2)}R
            </div>
            <div className="chip-lbl">Sum R</div>
          </div>
        </div>
        {strat.length === 0 ? (
          <div className="empty" style={{ marginTop: 10 }}>No closed trades in range</div>
        ) : (
          <table className="ab-table" style={{ marginTop: 10 }}>
            <thead>
              <tr><th>Strategy</th><th>n</th><th>WR</th><th>ΣR</th><th>TP/SL</th></tr>
            </thead>
            <tbody>
              {strat.map(s => (
                <tr key={s.name}>
                  <td style={{ textAlign: "left" }}>{s.name}</td>
                  <td>{s.n}</td>
                  <td className={s.wr > 0.6 ? "pos" : s.wr < 0.5 ? "neg" : ""}>
                    {Math.round(s.wr * 100)}%
                  </td>
                  <td className={rClass(s.sum_r)}>{s.sum_r.toFixed(2)}</td>
                  <td>{s.tp}/{s.sl}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </>
    );
  }

  return (
    <table className="ab-table">
      <thead>
        <tr>
          <th>Symbol</th>
          <th colSpan={3}>M1 (Claude)</th>
          <th colSpan={3}>M2 (Rules)</th>
        </tr>
        <tr>
          <th />
          <th>n</th><th>WR</th><th>ΣR</th>
          <th>n</th><th>WR</th><th>ΣR</th>
        </tr>
      </thead>
      <tbody>
        {SYMBOLS.map(sym => {
          const row = abStats[sym] || {};
          const m1 = row.m1 || {};
          const m2 = row.m2 || {};
          return (
            <tr key={sym}>
              <td>{sym.replace("USDT", "")}</td>
              <td>{m1.n ?? 0}</td>
              <td className={m1.wr > 0.6 ? "pos" : m1.wr < 0.5 ? "neg" : ""}>
                {m1.wr !== undefined ? `${Math.round(m1.wr * 100)}%` : "—"}
              </td>
              <td className={rClass(m1.total_r)}>
                {m1.total_r !== undefined ? m1.total_r.toFixed(1) : "—"}
              </td>
              <td>{m2.n ?? 0}</td>
              <td className={m2.wr > 0.6 ? "pos" : m2.wr < 0.5 ? "neg" : ""}>
                {m2.wr !== undefined ? `${Math.round(m2.wr * 100)}%` : "—"}
              </td>
              <td className={rClass(m2.total_r)}>
                {m2.total_r !== undefined ? m2.total_r.toFixed(1) : "—"}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
