import type {PortfolioResearchReport, ReplayBook} from "./research-portfolio";

const fields = ["reference_pnl_inr", "spread_cost_inr", "slippage_cost_inr", "fees_inr", "realized_pnl_inr", "unrealized_pnl_inr", "net_pnl_inr", "contribution_fraction"] as const;
export type ContributionValues = Record<typeof fields[number], string | null>;
export type ReplayAttribution = {
  method: "inception_cash_flow_midpoint_v1"; status: "complete" | "incomplete";
  rows: Array<ContributionValues & {symbol: string; quantity: number}>;
  totals: ContributionValues; reconciliation_difference_inr: string | null;
};

function check(condition: unknown): asserts condition {if (!condition) throw new Error("portfolio_accounting_mismatch");}
function same(actual: string | number | null, expected: number | null): boolean {
  if (actual === null || expected === null) return actual === expected;
  return Number.isFinite(Number(actual)) && Number.isFinite(expected) && Math.abs(Number(actual) - expected) <= 1e-9 * Math.max(1, Math.abs(expected));
}
function exact(value: unknown, keys: readonly string[]): asserts value is Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  check(Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)));
}

/** Independently reconstruct the exported accounting; a payload hash alone is not validation. */
export function verifyPortfolioAccounting(book: ReplayBook, report: PortfolioResearchReport) {
  const v2 = report.schema === "pramana.portfolio_workspace.v2";
  const initial = Number(report.config.starting_cash_inr);
  const states = new Map<string, {quantity: number; cost: number; cashFlow: number; referenceFlow: number; realized: number; fees: number; spread: number; slippage: number}>();
  let lastTime = -Infinity;
  for (const f of book.fills) {
    const at = Date.parse(f.at);
    check(at >= lastTime); lastTime = at;
    const s = states.get(f.symbol) ?? {quantity: 0, cost: 0, cashFlow: 0, referenceFlow: 0, realized: 0, fees: 0, spread: 0, slippage: 0};
    const qty = f.quantity, price = Number(f.price), fee = Number(f.fee_inr), buy = f.side === "BUY";
    check(qty <= report.config.max_order_quantity && same(fee, qty * price * Number(report.config.fee_bps) / 10000));
    if (v2) {
      const bid = Number(f.quote_bid), ask = Number(f.quote_ask);
      check(Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask >= bid);
      check(same(price, (buy ? ask : bid) * (1 + (buy ? 1 : -1) * Number(report.config.slippage_bps) / 10000)));
      s.referenceFlow += (buy ? -1 : 1) * qty * (bid + ask) / 2;
      s.spread += qty * (ask - bid) / 2;
      s.slippage += qty * (buy ? price - ask : bid - price);
    }
    if (buy) {s.quantity += qty; s.cost += qty * price + fee;}
    else {
      check(qty <= s.quantity);
      const cost = s.cost * qty / s.quantity;
      s.quantity -= qty; s.cost -= cost; s.realized += qty * price - fee - cost;
    }
    s.cashFlow += (buy ? -1 : 1) * qty * price - fee; s.fees += fee;
    states.set(f.symbol, s);
  }
  const held = new Map(book.holdings.map(h => [h.symbol, h]));
  check(held.size === [...states.values()].filter(s => s.quantity > 0).length);
  const stale = book.holdings.filter(h => !h.mark_fresh).map(h => h.symbol).sort();
  check(JSON.stringify(stale) === JSON.stringify([...book.stale_symbols].sort()));
  check(book.current_equity_inr === null ? !book.curve.length || stale.length > 0 : stale.length === 0);
  const rows = [...states.entries()].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([symbol, s]) => {
    const h = held.get(symbol);
    check(s.quantity > 0 ? h && h.quantity === s.quantity && same(h.cost_inr, s.cost) : !h);
    const value = h ? h.market_value_inr === null ? null : Number(h.market_value_inr) : 0;
    const net = value === null ? null : s.cashFlow + value;
    const unrealized = value === null ? null : value - s.cost;
    if (h) check(same(h.unrealized_pnl_inr, unrealized));
    return {symbol, quantity: s.quantity, reference_pnl_inr: value === null ? null : s.referenceFlow + value,
      spread_cost_inr: s.spread, slippage_cost_inr: s.slippage, fees_inr: s.fees,
      realized_pnl_inr: s.realized, unrealized_pnl_inr: unrealized,
      net_pnl_inr: net, contribution_fraction: net === null ? null : net / initial};
  });
  const complete = book.current_equity_inr !== null;
  const totals = Object.fromEntries(fields.map(k => [k, complete || ["spread_cost_inr", "slippage_cost_inr", "fees_inr", "realized_pnl_inr"].includes(k) ? rows.reduce((sum, r) => sum + Number(r[k]), 0) : null])) as Record<typeof fields[number], number | null>;
  check(same(book.cash_inr, initial + [...states.values()].reduce((sum, s) => sum + s.cashFlow, 0)));
  for (const k of ["fees_inr", "realized_pnl_inr", "unrealized_pnl_inr"] as const) check(same(book[k], totals[k]));
  check(same(book.net_return_fraction, totals.contribution_fraction));
  const equityPnl = complete ? Number(book.current_equity_inr) - initial : null;
  check(same(totals.net_pnl_inr, equityPnl));
  if (!v2) return; // Legacy reports have no fill-quote reference; do not invent execution attribution.
  const a = book.attribution;
  exact(a, ["method", "status", "rows", "totals", "reconciliation_difference_inr"]);
  check(a.method === "inception_cash_flow_midpoint_v1" && a.status === (complete ? "complete" : "incomplete"));
  check(Array.isArray(a.rows) && a.rows.length === rows.length);
  const verifyValues = (actual: Record<string, unknown>, expected: Record<typeof fields[number], number | null>) => {
    for (const k of fields) {
      check(actual[k] === null || typeof actual[k] === "string" && actual[k].trim() !== "");
      check(same(actual[k] as string | null, expected[k]));
    }
  };
  for (const [i, row] of a.rows.entries()) {
    exact(row, ["symbol", "quantity", ...fields]);
    check(row.symbol === rows[i].symbol && row.quantity === rows[i].quantity);
    verifyValues(row, rows[i]);
  }
  exact(a.totals, fields); verifyValues(a.totals, totals);
  check(a.reconciliation_difference_inr === null || typeof a.reconciliation_difference_inr === "string");
  // Python Decimal retains a sub-paisa residual; JS reconstruction uses a documented relative tolerance.
  check(same(a.reconciliation_difference_inr as string | null, complete ? 0 : null));
}
