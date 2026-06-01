import axios from "axios";

export async function fetchState(symbol, tf, minutes, footprint = false) {
  // minutes = 1440 → session-aligned window (XAU 03:30 IST, BTC 05:30 IST).
  const params = { symbol, tf, minutes };
  if (footprint) params.footprint = "true";
  if (minutes === 1440) params.session = "today";
  const { data } = await axios.get("/dashboard/state", { params });
  return data;
}

export async function fetchStrategyTrades(name, symbol, tf, source = "all") {
  const params = { symbol, tf, source };
  const { data } = await axios.get(`/strategies/${name}/trades`, { params });
  return data?.trades ?? [];
}
