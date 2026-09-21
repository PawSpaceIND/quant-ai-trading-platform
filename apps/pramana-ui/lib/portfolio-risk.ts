import type { Portfolio } from "./types";

/**
 * The engine's own daily-loss breaker, reproduced exactly so the page cannot draw a
 * different line from the one that halts trading. The daemon computes
 * `opening = total_equity - daily_total_pnl` and engages the kill switch when
 * `-daily_total_pnl / opening >= limit`, so a profitable day consumes none of it.
 *
 * Null whenever opening equity is not a positive number, or the engine withheld the day's
 * P&L: with no denominator there is no fraction, and the engine does not test the breaker
 * there either. The telemetry publishes a null dailyPnl whenever totals are withheld, so
 * this is a real state and not a defensive check.
 */
export function dailyLossUsed(p: Pick<Portfolio, "totalEquity" | "dailyPnl">): number | null {
  const daily = p.dailyPnl;
  if (!Number.isFinite(p.totalEquity) || typeof daily !== "number" || !Number.isFinite(daily)) return null;
  const opening = p.totalEquity - daily;
  if (!Number.isFinite(opening) || opening <= 0) return null;
  return Math.max(0, -daily / opening);
}

/**
 * Next per-holding overrides after the operator types `raw` into one shock box.
 *
 * An empty box is not a shock of zero. `Number("")` is 0 and finite, so reading the box
 * with Number() pinned the holding at 0% the moment it was cleared to be retyped: the
 * holding silently left the scenario, the P&L moved, and the override stuck until Reset.
 * An empty or unparseable box means no override, so the holding follows the common shock
 * until a number is actually entered. Out-of-range input is clamped, as it always was.
 */
export function applyShockEdit(overrides: Record<string, number>, key: string, raw: string): Record<string, number> {
  const value = raw.trim() === "" ? Number.NaN : Number(raw);
  if (!Number.isFinite(value)) {
    if (!Object.hasOwn(overrides, key)) return overrides;
    const {[key]: _cleared, ...rest} = overrides;
    return rest;
  }
  return {...overrides, [key]: Math.max(-100, Math.min(100, value))};
}

export function holdingKey(h: Portfolio["holdings"][number]) {
  return `${h.market}:${h.assetClass}:${h.symbol}`;
}

// Descriptive cash-equity diagnostics, not covariance, VaR or guaranteed stop fills.
export function portfolioRisk(p: Portfolio, parallelShock: number, overrides: Record<string, number> = {}) {
  if (!Number.isFinite(p.totalEquity) || p.totalEquity <= 0 ||
      !Number.isFinite(parallelShock) || parallelShock < -100 || parallelShock > 100) {
    return { status: "unavailable" as const, reason: "Positive equity and a valid shock are required." };
  }
  if (p.holdings.some(h => !["EQUITY", "ETF"].includes(h.assetClass) ||
      !Number.isFinite(h.marketValue) || h.marketValue < 0 ||
      !Number.isFinite(h.quantity) || h.quantity <= 0 ||
      !Number.isFinite(h.markPrice) || h.markPrice <= 0 ||
      Math.abs(h.marketValue - h.quantity * h.markPrice) > Math.max(.01, h.marketValue * 1e-8))) {
    return { status: "unavailable" as const, reason: "Requires consistent positive cash-equity/ETF holdings; contracts and short positions are unsupported." };
  }
  const keys = p.holdings.map(holdingKey);
  if (new Set(keys).size !== keys.length || new Set(p.holdings.map(h => h.market)).size > 1) {
    return { status: "unavailable" as const, reason: "Duplicate holdings or mixed markets need reconciled valuation before aggregation." };
  }
  const rows = p.holdings.map(h => {
    const key = holdingKey(h);
    const shock = Object.hasOwn(overrides, key) ? overrides[key] : parallelShock;
    const validStop = typeof h.stopPrice === "number" && Number.isFinite(h.stopPrice) && h.stopPrice > 0;
    return {
      key, symbol: h.symbol, market: h.market, marketValue: h.marketValue,
      weight: h.marketValue / p.totalEquity, shock, pnl: h.marketValue * shock / 100,
      stopState: !validStop ? "missing" : h.stopPrice! >= h.markPrice ? "at_or_breached" : "below_mark",
      stopDownside: validStop ? h.quantity * Math.max(0, h.markPrice - h.stopPrice!) : null,
      fresh: h.fresh === true && p.markMode === "engine_live" && ["ok", "degraded"].includes(p.status),
    };
  });
  if (rows.some(r => !Number.isFinite(r.shock) || r.shock < -100 || r.shock > 100)) {
    return { status: "unavailable" as const, reason: "Each price shock must be between −100% and +100%." };
  }
  const exposure = rows.reduce((sum, r) => sum + r.marketValue, 0);
  const pnl = rows.reduce((sum, r) => sum + r.pnl, 0);
  const hhi = exposure > 0 ? rows.reduce((sum, r) => sum + (r.marketValue / exposure) ** 2, 0) : null;
  return {
    status: "ok" as const, rows, exposure, pnl, equityImpact: pnl / p.totalEquity,
    scenarioEquity: p.totalEquity + pnl,
    largestEquityWeight: rows.length ? Math.max(...rows.map(r => r.weight)) : 0,
    effectiveHoldings: hhi ? 1 / hhi : null,
    grossEquityWeight: exposure / p.totalEquity,
    // Null, not zero, when no holding has a recorded stop: the downside is unknown, not
    // absent. A breached stop still contributes a real zero, so only an all-missing book
    // withholds the figure.
    recordedStopDownside: rows.length > 0 && rows.every(r => r.stopDownside === null)
      ? null
      : rows.reduce((sum, r) => sum + (r.stopDownside ?? 0), 0),
    missingStops: rows.filter(r => r.stopState === "missing").length,
    breachedStops: rows.filter(r => r.stopState === "at_or_breached").length,
    staleMarks: rows.filter(r => !r.fresh).length,
  };
}
