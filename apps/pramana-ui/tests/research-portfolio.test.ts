import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {createHash} from "node:crypto";
import type {PortfolioResearchReport, ReplayBook} from "../lib/research-portfolio";

const directory = fs.mkdtempSync(path.join(os.tmpdir(), "portfolio-research-test-"));
process.env.PRAMANA_TENANT_ID = "default";
process.env.PRAMANA_LEDGER_PATH = path.join(directory, "missing-ledger.db");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(directory, "missing-market.json");
process.env.PRAMANA_CONSOLE_DB = path.join(directory, "console.db");
process.env.PRAMANA_PROOF_DIR = path.join(directory, "missing-proofs");
const file = path.join(directory, "report.json"), evidenceHash = "f".repeat(64);
after(() => fs.rmSync(directory, {recursive: true, force: true}));

function report(): PortfolioResearchReport {
  const active: ReplayBook = {
    cash_inr: "900", current_equity_inr: null, net_return_fraction: null, realized_pnl_inr: "0", unrealized_pnl_inr: null,
    fees_inr: "0", peak_observed_equity_inr: "1000", current_drawdown_fraction: null, max_observed_drawdown_fraction: "0",
    unvalued_observations: 1, halted: false, stale_symbols: ["NSE:TEST"],
    holdings: [{symbol: "NSE:TEST", quantity: 1, cost_inr: "100", mark_fresh: false, last_bid: "100", quote_at: "2000-01-01T04:00:02Z", market_value_inr: null, unrealized_pnl_inr: null}],
    fills: [{order_id: "order-1", quote_id: "quote-2", symbol: "NSE:TEST", side: "BUY", quantity: 1, price: "100", fee_inr: "0", at: "2000-01-01T04:00:02Z"}],
    pending_orders: [], cancelled_orders: [],
    curve: ["2000-01-01T04:00:00Z", "2000-01-01T04:00:01Z", "2000-01-01T04:00:02Z", "2000-01-01T04:02:02Z"].map((at, event_index) => ({event_index, at, equity_inr: event_index === 3 ? null : "1000", drawdown_fraction: event_index === 3 ? null : "0", stale_symbols: event_index === 3 ? ["NSE:TEST"] : []})),
  };
  const cash: ReplayBook = {...structuredClone(active), cash_inr: "1000", current_equity_inr: "1000", net_return_fraction: "0", unrealized_pnl_inr: "0", current_drawdown_fraction: "0", unvalued_observations: 0, stale_symbols: [], holdings: [], fills: [], curve: active.curve.map(p => ({...p, equity_inr: "1000", drawdown_fraction: "0", stale_symbols: []}))};
  return {schema: "pramana.portfolio_workspace.v1", tenant_id: "default", name: "Synthetic portfolio", generated_at: new Date().toISOString(), as_of: "2000-01-01T04:02:02Z", evidence_sha256: evidenceHash, implementation: {source_sha256: "e".repeat(64), python_version: "3.12"}, mode: "research_simulation", status: "insufficient_evidence", automatic_promotion: false, event_count: 4, quote_events: 2, order_events: 1, symbols: ["NSE:TEST"], config: {starting_cash_inr: "1000", fee_bps: "0", slippage_bps: "0", max_position_fraction: "1", max_gross_fraction: "1", max_drawdown_fraction: ".2", max_quote_age_seconds: 60, order_ttl_seconds: 60, max_order_quantity: 10}, books: {active, cash}, limitations: ["Synthetic supplied-event simulation; not current account performance."]};
}
function envelope(body: unknown) {const payload = JSON.stringify(body); return JSON.stringify({payload, sha256: createHash("sha256").update(payload).digest("hex")});}
const contributionCases = JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/portfolio-attribution.json", import.meta.url), "utf8")) as Record<string, PortfolioResearchReport>;

test("Python contribution evidence reconciles in Node across partial fills, closed positions, gaps and cash", async () => {
  const {parsePortfolioResearch} = await import("../lib/research-portfolio");
  for (const [name, body] of Object.entries(contributionCases)) {
    const result = parsePortfolioResearch(envelope(body), "default");
    assert.deepEqual(result, body, name);
  }
  const a = contributionCases.fresh.books.active.attribution!;
  assert.equal(Number(a.totals.net_pnl_inr), 31.750065);
  assert.equal(Number(a.totals.reference_pnl_inr), 50);
  assert.equal(Number(a.totals.spread_cost_inr), 14);
  assert.equal(Number(a.totals.slippage_cost_inr), 3.035);
  assert.equal(Number(a.totals.fees_inr), 1.214935);
});

test("a recomputed hash cannot conceal accounting, reference-quote or attribution inconsistencies", async () => {
  const {parsePortfolioResearch} = await import("../lib/research-portfolio");
  const changes: Array<(r: PortfolioResearchReport) => void> = [
    r => {r.books.active.cash_inr = "9999";},
    r => {r.books.active.realized_pnl_inr = "99";},
    r => {r.books.active.unrealized_pnl_inr = "99";},
    r => {r.books.active.fees_inr = "0";},
    r => {r.books.active.holdings[0].cost_inr = "1";},
    r => {r.books.active.holdings[0].unrealized_pnl_inr = "10000";},
    r => {r.books.active.fills[0].quote_ask = "500";},
    r => {r.books.active.fills[0].quote_bid = "0";},
    r => {r.books.active.fills[0].fee_inr = "0";},
    r => {r.books.active.fills[0].side = "SELL";},
    r => {r.books.active.fills.reverse();},
    r => {r.books.active.attribution!.rows.pop();},
    r => {r.books.active.attribution!.rows[0].net_pnl_inr = "0";},
    r => {r.books.active.attribution!.rows[0].fees_inr = "0";},
    r => {r.books.active.attribution!.totals.net_pnl_inr = "50";},
    r => {r.books.active.attribution!.status = "incomplete";},
    r => {r.books.active.attribution!.reconciliation_difference_inr = "1";},
    r => {delete r.books.active.attribution;},
    r => {r.schema = "pramana.portfolio_workspace.v1";},
  ];
  for (const change of changes) {
    const r = structuredClone(contributionCases.fresh); change(r);
    assert.throws(() => parsePortfolioResearch(envelope(r), "default"));
  }
  const stale = structuredClone(contributionCases.stale);
  stale.books.active.attribution!.totals.net_pnl_inr = "31.750065";
  assert.throws(() => parsePortfolioResearch(envelope(stale), "default"));
  const legacy = report(); legacy.books.active.cash_inr = "901";
  assert.throws(() => parsePortfolioResearch(envelope(legacy), "default"));
});

test("Atlas receives reconciled contribution rows and known costs without execution details", async () => {
  fs.writeFileSync(file, envelope(contributionCases.partial));
  process.env.PRAMANA_PORTFOLIO_RESEARCH_REPORT = file;
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network-key";
  const {generateAnswer, conversations} = await import("../lib/copilot");
  let supplied = "";
  const transport: typeof fetch = async (_url, init) => {supplied = JSON.parse(String(init?.body)).system; return Response.json({content: [{type: "text", text: "Synthetic contribution response"}], usage: {input_tokens: 10, output_tokens: 5}});};
  try {
    const response = await generateAnswer("Explain contribution and missing marks", undefined, "synthetic-contribution-context", transport);
    assert.equal(response.status, "complete");
    const context = JSON.parse(conversations(response.id)[0].context!);
    const b = context.researchPortfolio.report.books.active;
    assert.deepEqual(b.attribution, contributionCases.partial.books.active.attribution);
    assert.equal(b.attribution.status, "incomplete");
    assert.equal(b.attribution.totals.net_pnl_inr, null);
    assert.equal(b.fills, undefined);
    assert(supplied.includes(JSON.stringify(context)));
  } finally {delete process.env.ANTHROPIC_API_KEY;}
});

test("portfolio snapshot preserves missing valuations instead of carrying an old equity forward", async () => {
  const {parsePortfolioResearch} = await import("../lib/research-portfolio");
  const value = parsePortfolioResearch(envelope(report()), "default");
  assert.equal(value.books.active.current_equity_inr, null);
  assert.equal(value.books.active.net_return_fraction, null);
  assert.equal(value.books.active.curve.at(-1)!.equity_inr, null);
  assert.equal(value.books.active.unvalued_observations, 1);
  assert.equal(value.books.cash.current_equity_inr, "1000");
  assert.equal(value.automatic_promotion, false);
});

test("cross-tenant, corrupted, private-field and falsely completed portfolio evidence is rejected", async () => {
  const {parsePortfolioResearch} = await import("../lib/research-portfolio");
  assert.throws(() => parsePortfolioResearch(envelope(report()), "other"));
  const corrupt = JSON.parse(envelope(report())); corrupt.sha256 = "a".repeat(64);
  assert.throws(() => parsePortfolioResearch(JSON.stringify(corrupt), "default"));
  const changes = [
    (r: PortfolioResearchReport) => Object.assign(r, {raw_events: "PRIVATE_SOURCE"}),
    (r: PortfolioResearchReport) => Object.assign(r, {automatic_promotion: true}),
    (r: PortfolioResearchReport) => {r.books.active.current_equity_inr = "1000";},
    (r: PortfolioResearchReport) => {r.books.active.unvalued_observations = 0;},
    (r: PortfolioResearchReport) => {r.books.active.max_observed_drawdown_fraction = ".1";},
    (r: PortfolioResearchReport) => {r.books.active.holdings[0].market_value_inr = "100";},
    (r: PortfolioResearchReport) => {r.books.active.stale_symbols = [];},
    (r: PortfolioResearchReport) => {r.books.active.curve[0].at = "2000-01-02T04:00:00Z";},
    (r: PortfolioResearchReport) => Object.assign(r.books.active.fills[0], {decision_ref: "PRIVATE_DECISION"}),
  ];
  for (const change of changes) {const value = report(); change(value); assert.throws(() => parsePortfolioResearch(envelope(value), "default"));}
});

test("unconfigured and damaged report files have explicit states without creating storage", async () => {
  const {readPortfolioResearch} = await import("../lib/research-portfolio");
  fs.rmSync(file, {force: true});
  delete process.env.PRAMANA_PORTFOLIO_RESEARCH_REPORT;
  assert.equal(readPortfolioResearch().status, "unavailable");
  process.env.PRAMANA_PORTFOLIO_RESEARCH_REPORT = file;
  assert.equal(readPortfolioResearch().status, "invalid");
  assert(!fs.existsSync(file));
  fs.writeFileSync(file, envelope(report()));
  assert.equal(readPortfolioResearch().status, "published");
  fs.writeFileSync(file, "{}");
  assert.equal(readPortfolioResearch().report, null);
});

test("Atlas persists the same bounded simulation context with gaps and no invented current result", async () => {
  fs.writeFileSync(file, envelope(report()));
  process.env.PRAMANA_PORTFOLIO_RESEARCH_REPORT = file;
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network-key";
  const {generateAnswer, conversations} = await import("../lib/copilot");
  let supplied = "";
  const transport: typeof fetch = async (_url, init) => {supplied = JSON.parse(String(init?.body)).system; return Response.json({content: [{type: "text", text: "Synthetic response"}], usage: {input_tokens: 10, output_tokens: 5}});};
  const response = await generateAnswer("Explain the replay", undefined, "synthetic-portfolio-context", transport);
  assert.equal(response.status, "complete");
  const context = JSON.parse(conversations(response.id)[0].context!);
  const replay = context.researchPortfolio.report;
  assert.equal(replay.evidence_sha256, evidenceHash);
  assert.equal(replay.mode, "research_simulation");
  assert.equal(replay.books.active.current_equity_inr, null);
  assert.equal(replay.books.active.unvalued_observations, 1);
  assert.equal(replay.books.active.curve, undefined);
  assert.equal(replay.books.active.fills, undefined);
  assert.equal(replay.books.active.fill_count, 1);
  assert.equal(replay.curve_and_order_details_included, false);
  assert(supplied.includes(JSON.stringify(context)));
  delete process.env.ANTHROPIC_API_KEY;
});
