function LegBubbles({ legPrices = [], maxLegs = 5, side }) {
  const filled = legPrices.filter(p => p !== null && p !== undefined).length;
  return (
    <div className="leg-bubbles">
      {Array.from({ length: maxLegs }).map((_, i) => (
        <div
          key={i}
          className={`leg-bubble ${i < filled ? `filled ${side}` : "pending"}`}
          title={legPrices[i] ? legPrices[i].toFixed(2) : `L${i + 1} pending`}
        >
          {i + 1}
        </div>
      ))}
    </div>
  );
}

function RBar({ value, max }) {
  const pct = Math.min(Math.abs(value) / (max || 1) * 100, 100);
  return (
    <div className="r-bar-wrap">
      <div className={`r-bar ${value >= 0 ? "pos" : "neg"}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export default function Positions({ positions }) {
  if (!positions?.length) return <div className="empty">No open positions</div>;

  return (
    <div>
      {positions.map(p => {
        // Rough unrealized R: current leg count × placeholder (no live price here)
        const filledLegs = (p.leg_prices || []).filter(x => x !== null).length;
        const maxLegs = p.max_legs || 5;
        const riskPerLeg = p.avg_entry && p.stop_loss
          ? Math.abs(p.avg_entry - p.stop_loss)
          : null;
        const rewardTotal = p.avg_entry && p.take_profit
          ? Math.abs(p.take_profit - p.avg_entry)
          : null;
        const shownRR = riskPerLeg && rewardTotal ? (rewardTotal / riskPerLeg).toFixed(1) : "—";

        return (
          <div key={p.position_id} style={{ marginBottom: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className={`side-badge ${p.side}`}>{p.side}</span>
              <span style={{ color: "#aaa" }}>{p.symbol}</span>
              <span style={{ color: "#555", marginLeft: "auto", fontSize: 10 }}>
                R:R {shownRR}
              </span>
            </div>
            <div className="kv-row" style={{ marginTop: 2 }}>
              <span className="k">avg entry</span>
              <span className="v">{p.avg_entry?.toFixed(2)}</span>
            </div>
            <div className="kv-row">
              <span className="k">TP / SL</span>
              <span className="v pos">{p.take_profit?.toFixed(2)}</span>
              <span style={{ color: "#555", margin: "0 4px" }}>/</span>
              <span className="v neg">{p.stop_loss?.toFixed(2)}</span>
            </div>
            <LegBubbles legPrices={p.leg_prices} maxLegs={maxLegs} side={p.side} />
            <div style={{ color: "#555", fontSize: 9, marginTop: 2 }}>
              {filledLegs}/{maxLegs} legs filled
            </div>
          </div>
        );
      })}
    </div>
  );
}
