function Card({ title, dec, accentColor }) {
  if (!dec) {
    return (
      <div style={{ flex: 1, padding: "0 8px", borderRight: "1px solid var(--border)" }}>
        <div className="panel-title">{title}</div>
        <div className="empty">No decision yet</div>
      </div>
    );
  }

  const side = dec.side || "flat";
  const conf = dec.confidence ?? dec.score ?? 0;
  const confPct = Math.round(conf * 100);

  const fmt = (v) => (v !== null && v !== undefined ? v.toFixed(2) : "—");

  // For M2, bias_strength and votes summary; for M1, rationale
  const isM2 = !!dec.votes;

  return (
    <div style={{ flex: 1, padding: "0 8px", borderRight: "1px solid var(--border)", overflow: "hidden" }}>
      <div className="panel-title">{title}</div>

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        <span className={`side-badge ${side}`}>{side}</span>
        {dec.bias_strength && (
          <span style={{ color: accentColor, fontSize: 10 }}>
            bias {dec.bias_strength}/5
          </span>
        )}
        {isM2 && dec.score !== undefined && (
          <span style={{ color: "#666", fontSize: 10 }}>
            score {dec.score?.toFixed(2)}
          </span>
        )}
      </div>

      {/* confidence bar */}
      <div className="conf-bar-wrap">
        <div
          className={`conf-bar ${side}`}
          style={{ width: `${confPct}%` }}
        />
      </div>
      <div style={{ color: "#555", fontSize: 9, marginBottom: 4 }}>
        conf {confPct}%
      </div>

      {/* entry / SL / TP */}
      {dec.entry && (
        <div className="kv-row"><span className="k">entry</span><span className="v">{fmt(dec.entry)}</span></div>
      )}
      {dec.stop_loss && (
        <div className="kv-row"><span className="k">SL</span><span className="v neg">{fmt(dec.stop_loss)}</span></div>
      )}
      {dec.take_profit && (
        <div className="kv-row"><span className="k">TP</span><span className="v pos">{fmt(dec.take_profit)}</span></div>
      )}

      {/* rationale (M1) */}
      {dec.rationale && !isM2 && (
        <div className="rationale-text">{dec.rationale}</div>
      )}

      {/* top vote reason (M2) */}
      {isM2 && dec.votes?.length > 0 && (
        <div className="rationale-text">
          {dec.votes
            .filter(v => Math.abs(v.direction) > 0.1)
            .sort((a, b) => Math.abs(b.direction * b.strength) - Math.abs(a.direction * a.strength))
            .slice(0, 2)
            .map(v => `${v.module}: ${v.reason}`)
            .join(" · ")}
        </div>
      )}

      {/* M2 note */}
      {isM2 && dec.note && !dec.votes?.length && (
        <div className="rationale-text">{dec.note}</div>
      )}

      {/* validator rejection */}
      {dec.validator_reason && (
        <div className="veto-text">✗ {dec.validator_reason}</div>
      )}
    </div>
  );
}

export default function DecisionCards({ m1, m2 }) {
  return (
    <>
      <Card title="M1 — Claude" dec={m1} accentColor="var(--cyan)" />
      <Card title="M2 — Rules" dec={m2} accentColor="var(--yellow)" />
    </>
  );
}
