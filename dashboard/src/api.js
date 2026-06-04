import axios from "axios";

export async function fetchState(symbol, tf, minutes, footprint = false) {
  // minutes = 1440 → session-aligned window (XAU 03:30 IST, BTC 05:30 IST).
  const params = { symbol, tf, minutes };
  if (footprint) params.footprint = "true";
  if (minutes === 1440) params.session = "today";
  const { data } = await axios.get("/dashboard/state", { params });
  return data;
}

// Per-strategy closed-trade outcomes over a range. from/to = epoch seconds (optional).
export async function fetchStrategyOutcomes(from, to) {
  const params = {};
  if (from !== undefined && from !== null) params.from = from;
  if (to !== undefined && to !== null) params.to = to;
  const { data } = await axios.get("/strategies/outcomes", { params });
  return data?.strategies ?? [];
}

export async function fetchStrategyTrades(name, symbol, tf, source = "all") {
  const params = { symbol, tf, source };
  const { data } = await axios.get(`/strategies/${name}/trades`, { params });
  return data?.trades ?? [];
}

// Grid-mode (m1 / m2) trade history.
export async function fetchGridTrades(mode, symbol, tf) {
  const { data } = await axios.get(`/grid/${mode}/trades`, { params: { symbol, tf } });
  return data?.trades ?? [];
}

// Cycle history (grid recovery cycles, joined with position levels).
export async function fetchStrategyCycles(name, symbol, tf) {
  const { data } = await axios.get(`/strategies/${name}/cycles`, { params: { symbol, tf } });
  return data?.cycles ?? [];
}
export async function fetchGridCycles(mode, symbol, tf) {
  const { data } = await axios.get(`/grid/${mode}/cycles`, { params: { symbol, tf } });
  return data?.cycles ?? [];
}
