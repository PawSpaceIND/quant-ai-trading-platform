import {createHash} from "node:crypto";
import type {DatabaseSync} from "node:sqlite";
import {hasTable, tenantId, type CostRow, type LedgerRow} from "./db";
import type {Portfolio} from "./types";

export type PaperContributionRow = {
  symbol: string; market: string; assetClass: string; quantity: number;
  realizedGrossPnl: number; cashFees: number; realizedAfterFeesPnl: number;
  unrealizedPnl: number | null; netPnl: number | null; returnContribution: number | null;
  spread: number | null; slippage: number | null; beforeModeledCostsPnl: number | null;
  markState: "fresh" | "unavailable" | "closed"; markTimestamp: string | null;
};
export type PaperContributionReport = {
  schema: "pramana.paper_contribution.v1"; tenantId: string; currency: "INR";
  ledgerId: number; asOf: string; generatedAt: string; sourceSha256: string;
  startingCapital: number; fillCount: number; costRowCount: number; marksCurrent: boolean;
  costCoverage: {missingSpreadOrders: number; missingSlippageOrders: number; unknownNoncashRows: number};
  rows: PaperContributionRow[];
  totals: Omit<PaperContributionRow, "symbol" | "market" | "assetClass" | "quantity" | "markState" | "markTimestamp">;
  reconciliationDifference: number | null; limitations: string[];
};
export type PaperContributionState = {
  status: "available" | "incomplete" | "unavailable" | "outdated" | "invalid";
  detail: string; report: PaperContributionReport | null;
};
type Snapshot = Portfolio & {tenantId?: string; ledgerId?: number; allMarksFresh?: boolean};
type Position = {symbol: string; market: string; asset_class: string; quantity: number; average_price: string};
const unavailable = (detail: string): PaperContributionState => ({status: "unavailable", detail, report: null});
class ContributionError extends Error {}
function check(value: unknown, code: string): asserts value {if (!value) throw new ContributionError(code);}
function amount(value: unknown): number {
  check(typeof value === "number" || typeof value === "string" && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value), "Invalid numeric record");
  const n = Number(value); check(Number.isFinite(n), "Invalid numeric record"); return n;
}
function same(a: number, b: number): boolean {return Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= 1e-8 + Number.EPSILON * 32 * Math.max(1, Math.abs(a), Math.abs(b));}
const key = (symbol: string, market: string, asset: string) => JSON.stringify([symbol, market, asset]);
const LIMITATIONS = [
  "Recorded paper fills and charges, not real broker executions or independently verified returns.",
  "Cash fees are expensed immediately; average entry excludes fees. Net P&L already includes costs.",
  "Spread/slippage are recorded model estimates, not measured exchange execution quality or midpoint fills.",
  "Before-modeled-cost P&L adds back recorded friction on the same fills and marks; it is not an executable alternative.",
  "No external flows, dividends, corporate actions, FX, benchmark/sector/factor attribution or model/infrastructure costs.",
  "Internal consistency and local hashes cannot detect coordinated rewriting of all source records.",
];

/** Caller holds one read transaction for valuation, account, positions, fills and costs. */
export function readPaperContribution(db: DatabaseSync, portfolio: Snapshot): PaperContributionState {
  if (portfolio.markMode !== "engine_live") return unavailable("A matching engine valuation is required; ledger-fill and historical replay marks do not establish current account contribution.");
  if (!["paper_accounts", "paper_positions", "paper_ledger", "paper_cost_ledger"].every(t => hasTable(db, t))) return unavailable("Account, fill, position or cost storage is missing; no charges are assumed to be zero.");
  try {
    const account = db.prepare("SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id=?").get(tenantId) as {starting_capital: string; cash_balance: string} | undefined;
    if (!account) return unavailable("The selected paper tenant has no initialized account.");
    const entries = db.prepare("SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id LIMIT 20001").all(tenantId) as LedgerRow[];
    const costs = db.prepare("SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id LIMIT 200001").all(tenantId) as CostRow[];
    const positions = db.prepare("SELECT symbol,market,asset_class,quantity,average_price FROM paper_positions WHERE tenant_id=? ORDER BY symbol,market,asset_class LIMIT 1001").all(tenantId) as Position[];
    if (entries.length > 20000 || costs.length > 200000 || positions.length > 1000) return unavailable("Contribution exceeds the bounded pilot history limit; no truncated result is shown.");
    const head = entries.at(-1)?.id ?? 0;
    if (portfolio.ledgerId !== head) return {status: "outdated", detail: "The engine valuation precedes the current ledger. Publish a new valuation before joining it to these fills.", report: null};
    check(portfolio.tenantId === tenantId && portfolio.currency === "INR", "Valuation tenant or currency mismatch");
    const initial = amount(account.starting_capital), cash = amount(account.cash_balance);
    check(initial > 0 && same(initial, amount(portfolio.startingCapital)) && same(cash, amount(portfolio.cash)), "Account and valuation cash mismatch");
    const asOf = Date.parse(portfolio.updatedAt ?? "");
    check(Number.isFinite(asOf), "Invalid valuation timestamp");
    const snapshotCurrent = Date.now() - asOf <= 30000 && Date.now() - asOf >= -5000 && portfolio.status !== "stale";
    const states = new Map<string, {symbol: string; market: string; assetClass: string; quantity: number; average: number; realized: number; fees: number; spread: number; slippage: number; spreadMissing: number; slippageMissing: number; unknown: number}>();
    const orders = new Map<string, {state: string; at: number; codes: Set<string>}>();
    let expectedCash = initial, previousTime = -Infinity;
    for (const e of entries) {
      check(e.tenant_id === tenantId && e.status === "FILLED" && e.market === "INDIA" && ["EQUITY", "ETF"].includes(e.asset_class), "Unsupported fill scope or status");
      check(typeof e.symbol === "string" && e.symbol.length > 0 && typeof e.order_id === "string" && e.order_id.length > 0 && !orders.has(e.order_id) && Number.isSafeInteger(e.id) && e.id > 0, "Invalid or duplicate fill identity");
      const at = Date.parse(e.created_at), price = amount(e.fill_price), notional = amount(e.notional);
      check(Number.isSafeInteger(e.quantity) && e.quantity > 0 && price > 0 && same(notional, price * e.quantity), "Invalid fill geometry");
      check(Number.isFinite(at) && at >= previousTime && at <= asOf, "Fill chronology exceeds valuation"); previousTime = at;
      const k = key(e.symbol, e.market, e.asset_class);
      const s = states.get(k) ?? {symbol: e.symbol, market: e.market, assetClass: e.asset_class, quantity: 0, average: 0, realized: 0, fees: 0, spread: 0, slippage: 0, spreadMissing: 0, slippageMissing: 0, unknown: 0};
      if (e.side === "BUY") {s.average = (s.average * s.quantity + notional) / (s.quantity + e.quantity); s.quantity += e.quantity; expectedCash -= notional;}
      else {
        check(e.side === "SELL" && e.quantity <= s.quantity, "Uncovered or invalid sale");
        s.realized += (price - s.average) * e.quantity; s.quantity -= e.quantity; expectedCash += notional;
        if (!s.quantity) s.average = 0;
      }
      states.set(k, s); orders.set(e.order_id, {state: k, at, codes: new Set()});
    }
    if (states.size > 1000) return unavailable("Contribution exceeds the 1,000-instrument pilot limit; no rows are omitted.");
    for (const c of costs) {
      const o = orders.get(c.order_id), value = amount(c.amount);
      check(c.tenant_id === tenantId && o && typeof c.code === "string" && c.code.length > 0 && !o.codes.has(c.code), "Orphan or duplicate cost record");
      check(value >= 0 && [0, 1].includes(c.cash_debit) && Date.parse(c.created_at) === o.at, "Invalid cost value or time");
      const s = states.get(o.state)!; o.codes.add(c.code);
      if (c.cash_debit) {check(!["SPREAD", "SLIPPAGE"].includes(c.code), "Execution drag cannot also be a cash charge"); s.fees += value; expectedCash -= value;}
      else if (c.code === "SPREAD") s.spread += value;
      else if (c.code === "SLIPPAGE") s.slippage += value;
      else s.unknown++;
    }
    for (const o of orders.values()) {
      const s = states.get(o.state)!;
      if (!o.codes.has("SPREAD")) s.spreadMissing++;
      if (!o.codes.has("SLIPPAGE")) s.slippageMissing++;
    }
    check(same(cash, expectedCash), "Cash does not reconcile to recorded fills and charges");
    const persisted = new Map(positions.map(p => [key(p.symbol, p.market, p.asset_class), p]));
    const holdings = new Map(portfolio.holdings.map(h => [key(h.symbol, h.market, h.assetClass), h]));
    const openCount = [...states.values()].filter(s => s.quantity > 0).length;
    check(persisted.size === positions.length && holdings.size === portfolio.holdings.length && persisted.size === openCount && holdings.size === openCount, "Position coverage mismatch");
    let observedUnrealized = 0, observedValue = 0;
    const rows = [...states.entries()].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, s]): PaperContributionRow => {
      const p = persisted.get(k), h = holdings.get(k);
      let unrealized: number | null = 0, stamp: string | null = null, markState: PaperContributionRow["markState"] = "closed";
      if (s.quantity) {
        check(p && h && p.quantity === s.quantity && h.quantity === s.quantity && same(amount(p.average_price), s.average) && same(amount(h.averageEntry), s.average), "Position quantity or average cost mismatch");
        const mark = amount(h.markPrice), value = amount(h.marketValue), gain = amount(h.unrealizedPnl);
        check(mark > 0 && same(value, mark * s.quantity) && same(gain, (mark - s.average) * s.quantity), "Valuation holding arithmetic mismatch");
        observedValue += value; observedUnrealized += gain;
        stamp = h.markTimestamp ?? null;
        const age = asOf - Date.parse(stamp ?? "");
        const fresh = snapshotCurrent && h.fresh === true && h.markSource === "live_tick" && age >= 0 && age <= 120000;
        unrealized = fresh ? gain : null; markState = fresh ? "fresh" : "unavailable";
      } else check(!p && !h, "Closed position remains in holdings");
      const realized = s.realized - s.fees, net = unrealized === null ? null : realized + unrealized;
      const spread = s.spreadMissing || s.unknown ? null : s.spread, slippage = s.slippageMissing || s.unknown ? null : s.slippage;
      return {symbol: s.symbol, market: s.market, assetClass: s.assetClass, quantity: s.quantity,
        realizedGrossPnl: s.realized, cashFees: s.fees, realizedAfterFeesPnl: realized, unrealizedPnl: unrealized,
        netPnl: net, returnContribution: net === null ? null : net / initial,
        spread, slippage, beforeModeledCostsPnl: net === null || spread === null || slippage === null ? null : net + s.fees + spread + slippage,
        markState, markTimestamp: stamp};
    });
    const sum = (k: keyof PaperContributionReport["totals"]) => rows.reduce((total, r) => total + (r[k] ?? 0), 0);
    check(same(amount(portfolio.realizedPnl), sum("realizedAfterFeesPnl")) && same(amount(portfolio.unrealizedPnl), observedUnrealized) && same(amount(portfolio.totalEquity), cash + observedValue), "Portfolio P&L or equity does not reconcile");
    const marksCurrent = snapshotCurrent && ["ok", "degraded"].includes(portfolio.status) && rows.every(r => r.markState !== "unavailable");
    const costCoverage = {missingSpreadOrders: [...states.values()].reduce((n,s) => n + s.spreadMissing, 0), missingSlippageOrders: [...states.values()].reduce((n,s) => n + s.slippageMissing, 0), unknownNoncashRows: [...states.values()].reduce((n,s) => n + s.unknown, 0)};
    const costsComplete = Object.values(costCoverage).every(v => v === 0);
    const totals = {realizedGrossPnl: sum("realizedGrossPnl"), cashFees: sum("cashFees"), realizedAfterFeesPnl: sum("realizedAfterFeesPnl"),
      unrealizedPnl: marksCurrent ? sum("unrealizedPnl") : null, netPnl: marksCurrent ? sum("netPnl") : null,
      returnContribution: marksCurrent ? sum("netPnl") / initial : null,
      spread: costCoverage.missingSpreadOrders || costCoverage.unknownNoncashRows ? null : sum("spread"),
      slippage: costCoverage.missingSlippageOrders || costCoverage.unknownNoncashRows ? null : sum("slippage"),
      beforeModeledCostsPnl: marksCurrent && costsComplete ? sum("beforeModeledCostsPnl") : null};
    const difference = marksCurrent ? totals.netPnl! - (portfolio.totalEquity - initial) : null;
    check(difference === null || same(totals.netPnl!, portfolio.totalEquity - initial), "Net contribution does not reconcile to equity change");
    const report: PaperContributionReport = {schema: "pramana.paper_contribution.v1", tenantId, currency: "INR", ledgerId: head,
      asOf: portfolio.updatedAt!, generatedAt: new Date().toISOString(), sourceSha256: createHash("sha256").update(JSON.stringify({account, entries, costs, positions, portfolio})).digest("hex"),
      startingCapital: initial, fillCount: entries.length, costRowCount: costs.length, marksCurrent, costCoverage,
      rows, totals, reconciliationDifference: difference, limitations: LIMITATIONS};
    return {status: marksCurrent && costsComplete ? "available" : "incomplete", detail: marksCurrent && costsComplete ? "Recorded instrument P&L reconciles to the matching engine valuation." : "Accounting reconciles, but stale marks or missing execution-cost records leave part of the contribution unavailable.", report};
  } catch (error) {
    return {status: "invalid", detail: `Paper contribution withheld: ${error instanceof ContributionError ? error.message : "storage could not be validated"}.`, report: null};
  }
}

export function paperContributionContext(state: PaperContributionState) {
  if (!state.report) return state;
  const rows = [...state.report.rows].sort((a,b) => Number(b.netPnl === null) - Number(a.netPnl === null) || Math.abs(b.netPnl ?? 0) - Math.abs(a.netPnl ?? 0));
  return {...state, report: {...state.report, rows: rows.slice(0, 50), omittedRows: Math.max(0, rows.length - 50), unavailableRows: rows.filter(r => r.netPnl === null).length, rowsOrderedBy: "missing_marks_then_absolute_net_pnl", totalsCoverAllRows: true}};
}
