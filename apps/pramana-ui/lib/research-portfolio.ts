import fs from "node:fs";
import {createHash} from "node:crypto";
import {tenantId} from "./db";

export type ReplayPoint = {event_index: number; at: string; equity_inr: string | null; drawdown_fraction: string | null; stale_symbols: string[]};
export type ReplayHolding = {symbol: string; quantity: number; cost_inr: string; mark_fresh: boolean; last_bid: string | null; quote_at: string | null; market_value_inr: string | null; unrealized_pnl_inr: string | null};
export type ReplayFill = {order_id: string; quote_id: string; symbol: string; side: "BUY" | "SELL"; quantity: number; price: string; fee_inr: string; at: string};
export type ReplayPending = {order_id: string; symbol: string; side: "BUY" | "SELL"; quantity: number; submitted_at: string};
export type ReplayCancellation = {order_id: string; symbol: string; side: "BUY" | "SELL"; quantity: number; reason: string};
export type ReplayBook = {
  cash_inr: string; current_equity_inr: string | null; net_return_fraction: string | null;
  realized_pnl_inr: string; unrealized_pnl_inr: string | null; fees_inr: string;
  peak_observed_equity_inr: string; current_drawdown_fraction: string | null;
  max_observed_drawdown_fraction: string | null; unvalued_observations: number;
  halted: boolean; stale_symbols: string[]; holdings: ReplayHolding[];
  fills: ReplayFill[]; pending_orders: ReplayPending[]; cancelled_orders: ReplayCancellation[]; curve: ReplayPoint[];
};
export type PortfolioResearchReport = {
  schema: "pramana.portfolio_workspace.v1"; tenant_id: string; name: string;
  generated_at: string; as_of: string | null; evidence_sha256: string;
  implementation: {source_sha256: string; python_version: string};
  mode: "research_simulation"; status: "insufficient_evidence"; automatic_promotion: false;
  event_count: number; quote_events: number; order_events: number; symbols: string[];
  config: {starting_cash_inr: string; fee_bps: string; slippage_bps: string;
    max_position_fraction: string; max_gross_fraction: string; max_drawdown_fraction: string;
    max_quote_age_seconds: number; order_ttl_seconds: number; max_order_quantity: number};
  books: Record<string, ReplayBook>; limitations: string[];
};
export type PortfolioResearchState = {status: "unavailable" | "invalid" | "published"; detail: string; report: PortfolioResearchReport | null};

function check(condition: unknown): asserts condition {if (!condition) throw new Error("invalid_portfolio_research");}
function exact(value: unknown, keys: readonly string[]): asserts value is Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  check(Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)));
}
function label(value: unknown): boolean {return typeof value === "string" && value.trim().length > 0 && value.length <= 1000;}
function count(value: unknown): boolean {return Number.isSafeInteger(value) && Number(value) >= 0;}
function decimal(value: unknown, signed = false): boolean {
  return typeof value === "string" && value.length < 100 && value.trim() !== "" && Number.isFinite(Number(value)) && (signed || Number(value) >= 0);
}
function fraction(value: unknown): boolean {return decimal(value) && Number(value) <= 1;}
function date(value: unknown): boolean {return typeof value === "string" && /(Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));}
function hash(value: unknown): boolean {return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);}
function list(value: unknown, limit: number): asserts value is unknown[] {check(Array.isArray(value) && value.length <= limit);}
function same(a: unknown, b: unknown): boolean {
  if (a === null || b === null) return a === b;
  return Math.abs(Number(a) - Number(b)) <= 1e-9 * Math.max(1, Math.abs(Number(a)), Math.abs(Number(b)));
}

export function parsePortfolioResearch(raw: string, tenant: string): PortfolioResearchReport {
  check(Buffer.byteLength(raw) <= 4_000_000);
  const envelope = JSON.parse(raw);
  exact(envelope, ["payload", "sha256"]);
  check(typeof envelope.payload === "string" && hash(envelope.sha256));
  check(createHash("sha256").update(envelope.payload).digest("hex") === envelope.sha256);
  const r = JSON.parse(envelope.payload);
  exact(r, ["schema", "tenant_id", "name", "generated_at", "as_of", "evidence_sha256", "implementation", "mode", "status", "automatic_promotion", "event_count", "quote_events", "order_events", "symbols", "config", "books", "limitations"]);
  check(r.schema === "pramana.portfolio_workspace.v1" && r.tenant_id === tenant && label(r.name));
  check(r.mode === "research_simulation" && r.status === "insufficient_evidence" && r.automatic_promotion === false);
  check(date(r.generated_at) && Date.parse(String(r.generated_at)) <= Date.now() + 300000 && hash(r.evidence_sha256));
  exact(r.implementation, ["source_sha256", "python_version"]);
  check(hash(r.implementation.source_sha256) && label(r.implementation.python_version));
  for (const key of ["event_count", "quote_events", "order_events"]) check(count(r[key]));
  check(Number(r.event_count) <= 5000 && Number(r.quote_events) + Number(r.order_events) <= Number(r.event_count));
  check(r.event_count === 0 ? r.as_of === null : date(r.as_of));
  list(r.symbols, 50); check(r.symbols.length > 0 && r.symbols.every(s => label(s) && String(s).startsWith("NSE:")) && new Set(r.symbols).size === r.symbols.length);
  list(r.limitations, 50); check(r.limitations.length > 0 && r.limitations.every(label));
  exact(r.config, ["starting_cash_inr", "fee_bps", "slippage_bps", "max_position_fraction", "max_gross_fraction", "max_drawdown_fraction", "max_quote_age_seconds", "order_ttl_seconds", "max_order_quantity"]);
  check(decimal(r.config.starting_cash_inr) && Number(r.config.starting_cash_inr) > 0);
  for (const key of ["fee_bps", "slippage_bps"]) check(decimal(r.config[key]) && Number(r.config[key]) < 10000);
  for (const key of ["max_position_fraction", "max_gross_fraction", "max_drawdown_fraction"]) check(fraction(r.config[key]) && Number(r.config[key]) > 0);
  for (const key of ["max_quote_age_seconds", "order_ttl_seconds", "max_order_quantity"]) check(count(r.config[key]) && Number(r.config[key]) > 0);
  check(r.books && typeof r.books === "object" && !Array.isArray(r.books));
  const books = Object.entries(r.books); check(books.length > 0 && books.length <= 16);
  for (const [name, b] of books) {
    check(label(name));
    exact(b, ["cash_inr", "current_equity_inr", "net_return_fraction", "realized_pnl_inr", "unrealized_pnl_inr", "fees_inr", "peak_observed_equity_inr", "current_drawdown_fraction", "max_observed_drawdown_fraction", "unvalued_observations", "halted", "stale_symbols", "holdings", "fills", "pending_orders", "cancelled_orders", "curve"]);
    for (const k of ["cash_inr", "fees_inr", "peak_observed_equity_inr"]) check(decimal(b[k]));
    check(decimal(b.realized_pnl_inr, true) && typeof b.halted === "boolean" && count(b.unvalued_observations));
    check(b.current_equity_inr === null || decimal(b.current_equity_inr));
    for (const k of ["net_return_fraction", "unrealized_pnl_inr"]) check(b[k] === null || decimal(b[k], true));
    for (const k of ["current_drawdown_fraction", "max_observed_drawdown_fraction"]) check(b[k] === null || fraction(b[k]));
    list(b.curve, 5000); check(b.curve.length === r.event_count);
    let previous = -Infinity, missing = 0, maximum: number | null = null;
    for (const [i, p] of b.curve.entries()) {
      exact(p, ["event_index", "at", "equity_inr", "drawdown_fraction", "stale_symbols"]);
      check(p.event_index === i && date(p.at) && Date.parse(String(p.at)) >= previous); previous = Date.parse(String(p.at));
      list(p.stale_symbols, 50); check(p.stale_symbols.every(s => (r.symbols as unknown[]).includes(s)));
      if (p.equity_inr === null) {check(p.drawdown_fraction === null && p.stale_symbols.length > 0); missing++;}
      else {check(decimal(p.equity_inr) && fraction(p.drawdown_fraction) && p.stale_symbols.length === 0); maximum = Math.max(maximum ?? 0, Number(p.drawdown_fraction));}
    }
    const latest = b.curve.at(-1) as ReplayPoint | undefined;
    check(b.unvalued_observations === missing && same(b.max_observed_drawdown_fraction, maximum));
    check(same(b.current_equity_inr, latest?.equity_inr ?? null) && same(b.current_drawdown_fraction, latest?.drawdown_fraction ?? null));
    check(!latest || Date.parse(latest.at) === Date.parse(String(r.as_of)));
    check(b.current_equity_inr === null ? b.net_return_fraction === null && b.unrealized_pnl_inr === null : same(b.net_return_fraction, Number(b.current_equity_inr) / Number(r.config.starting_cash_inr) - 1));
    list(b.stale_symbols, 50); check(JSON.stringify(b.stale_symbols) === JSON.stringify(latest?.stale_symbols ?? []));
    list(b.holdings, 50); const held = new Set();
    for (const h of b.holdings) {
      exact(h, ["symbol", "quantity", "cost_inr", "mark_fresh", "last_bid", "quote_at", "market_value_inr", "unrealized_pnl_inr"]);
      check(r.symbols.includes(h.symbol) && !held.has(h.symbol)); held.add(h.symbol);
      check(count(h.quantity) && Number(h.quantity) > 0 && decimal(h.cost_inr) && typeof h.mark_fresh === "boolean");
      check(h.last_bid === null || decimal(h.last_bid) && Number(h.last_bid) > 0);
      check(h.quote_at === null || date(h.quote_at));
      if (h.mark_fresh) {
        const age = Date.parse(String(r.as_of)) - Date.parse(String(h.quote_at));
        check(h.last_bid !== null && h.quote_at !== null && age >= 0 && age <= Number(r.config.max_quote_age_seconds) * 1000);
        check(decimal(h.market_value_inr) && decimal(h.unrealized_pnl_inr, true) && same(h.market_value_inr, Number(h.quantity) * Number(h.last_bid)));
      } else check(h.market_value_inr === null && h.unrealized_pnl_inr === null);
    }
    for (const k of ["fills", "pending_orders", "cancelled_orders"]) {list(b[k], 5000); const ids = new Set();
      for (const item of b[k]) {
        exact(item, k === "fills" ? ["order_id", "quote_id", "symbol", "side", "quantity", "price", "fee_inr", "at"] : k === "pending_orders" ? ["order_id", "symbol", "side", "quantity", "submitted_at"] : ["order_id", "symbol", "side", "reason", "quantity"]);
        check(label(item.order_id) && !ids.has(item.order_id)); ids.add(item.order_id);
        check(r.symbols.includes(item.symbol) && ["BUY", "SELL"].includes(String(item.side)) && count(item.quantity) && Number(item.quantity) > 0);
        if (k === "fills") check(label(item.quote_id) && decimal(item.price) && Number(item.price) > 0 && decimal(item.fee_inr) && date(item.at) && Date.parse(String(item.at)) <= Date.parse(String(r.as_of)));
        if (k === "pending_orders") check(date(item.submitted_at) && Date.parse(String(item.submitted_at)) <= Date.parse(String(r.as_of)));
        if (k === "cancelled_orders") check(label(item.reason));
      }
    }
  }
  return r as unknown as PortfolioResearchReport;
}

export function readPortfolioResearch(): PortfolioResearchState {
  const file = process.env.PRAMANA_PORTFOLIO_RESEARCH_REPORT;
  if (!file) return {status: "unavailable", detail: "No continuous portfolio replay has been published to this workspace.", report: null};
  try {
    const stat = fs.statSync(/* turbopackIgnore: true */ file); check(stat.isFile() && stat.size <= 4_000_000);
    const report = parsePortfolioResearch(fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"), tenantId);
    return {status: "published", detail: "Published simulation snapshot. Supplied inputs and strategy effectiveness still require qualification.", report};
  } catch {
    return {status: "invalid", detail: "The configured portfolio replay is missing, invalid or belongs to another tenant. Other workspace panels remain available.", report: null};
  }
}

export function portfolioResearchContext() {
  const state = readPortfolioResearch();
  if (!state.report) return state;
  const {books, ...report} = state.report;
  return {...state, report: {...report, curve_and_order_details_included: false,
    books: Object.fromEntries(Object.entries(books).map(([name, {curve, fills, pending_orders, cancelled_orders, ...book}]) =>
      [name, {...book, observations: curve.length, fill_count: fills.length, pending_order_count: pending_orders.length, cancelled_order_count: cancelled_orders.length}]))}};
}
