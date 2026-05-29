import {
  BarChart, Bar, XAxis, YAxis, Cell, ReferenceLine, Tooltip, ResponsiveContainer,
} from "recharts";

const MODULE_ORDER = ["wave", "confirmation", "fvg", "cvd", "sweep", "vp_shape", "vp_position"];

function getSortedVotes(votes) {
  const map = {};
  votes.forEach(v => { map[v.module] = v; });
  return MODULE_ORDER
    .filter(m => map[m])
    .map(m => map[m])
    .concat(votes.filter(v => !MODULE_ORDER.includes(v.module)));
}

function barColor(direction) {
  if (direction > 0.05)  return "#26a69a";
  if (direction < -0.05) return "#ef5350";
  return "#444";
}

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const v = payload[0].payload;
  return (
    <div style={{
      background: "#1a1a1a", border: "1px solid #2a2a2a",
      padding: "4px 8px", fontSize: 10, maxWidth: 180,
    }}>
      <div style={{ color: "#aaa" }}>{v.module}</div>
      <div>{v.reason}</div>
      <div style={{ color: "#666" }}>strength {v.strength?.toFixed(2)}</div>
    </div>
  );
};

export default function VoteBreakdown({ votes }) {
  if (!votes?.length) return <div className="empty">No votes yet</div>;

  const sorted = getSortedVotes(votes);
  const chartData = sorted.map(v => ({
    ...v,
    value: parseFloat((v.direction * v.strength).toFixed(3)),
  }));

  return (
    <ResponsiveContainer width="100%" height={160}>
      <BarChart
        data={chartData}
        layout="vertical"
        margin={{ top: 0, right: 8, bottom: 0, left: 4 }}
      >
        <XAxis
          type="number"
          domain={[-1, 1]}
          tick={{ fill: "#555", fontSize: 9 }}
          axisLine={{ stroke: "#2a2a2a" }}
          tickLine={false}
        />
        <YAxis
          type="category"
          dataKey="module"
          width={72}
          tick={{ fill: "#888", fontSize: 9 }}
          axisLine={false}
          tickLine={false}
        />
        <ReferenceLine x={0} stroke="#333" />
        <Tooltip content={<CustomTooltip />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <Bar dataKey="value" radius={[0, 2, 2, 0]}>
          {chartData.map((v, i) => (
            <Cell key={i} fill={barColor(v.direction)} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
