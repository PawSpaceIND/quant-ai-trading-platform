import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";

const folder = fs.mkdtempSync(path.join(os.tmpdir(), "paper-contribution-test-"));
const file = path.join(folder, "paper.sqlite");
process.env.PRAMANA_TENANT_ID = "default";
process.env.PRAMANA_LEDGER_PATH = file;
process.env.PRAMANA_CONSOLE_DB = path.join(folder, "console.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(folder, "missing-market.json");
process.env.PRAMANA_PROOF_DIR = path.join(folder, "missing-proofs");
const fixture = JSON.parse(fs.readFileSync(new URL("../../../tests/fixtures/paper-contribution.json", import.meta.url), "utf8"));
after(() => fs.rmSync(folder, {recursive: true, force: true}));

function source(name = "fresh") {
  fs.rmSync(file, {force: true});
  const db = new DatabaseSync(file);
  db.exec(`PRAGMA journal_mode=WAL;
    CREATE TABLE paper_accounts(tenant_id TEXT PRIMARY KEY,starting_capital TEXT,cash_balance TEXT,updated_at TEXT);
    CREATE TABLE paper_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,side TEXT,quantity INTEGER,fill_price TEXT,notional TEXT,status TEXT,created_at TEXT,stop_price TEXT,take_profit_price TEXT,margin_change TEXT,margin_provenance TEXT);
    CREATE TABLE paper_cost_ledger(id INTEGER PRIMARY KEY,order_id TEXT,tenant_id TEXT,code TEXT,amount TEXT,cash_debit INTEGER,created_at TEXT);
    CREATE TABLE paper_positions(tenant_id TEXT,symbol TEXT,market TEXT,asset_class TEXT,quantity INTEGER,average_price TEXT,stop_price TEXT,take_profit_price TEXT);
    CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,ledger_id INTEGER,payload TEXT);`);
  db.prepare("INSERT INTO paper_accounts VALUES ('default',?,?,?)").run(fixture.account.starting_capital, fixture.account.cash_balance, fixture.valuations[name].updatedAt);
  for (const [table, records] of [["paper_ledger", fixture.fills], ["paper_cost_ledger", fixture.costs], ["paper_positions", fixture.positions]] as const) {
    for (const row of records) db.prepare(`INSERT INTO ${table} (${Object.keys(row).join(",")}) VALUES (${Object.keys(row).map(() => "?").join(",")})`).run(...Object.values(row) as Array<string | number | null>);
  }
  db.prepare("INSERT INTO paper_live_valuations VALUES ('default',?,6,?)").run(fixture.valuations[name].updatedAt, JSON.stringify(fixture.valuations[name]));
  db.close();
}
function mutate(sql: string) {const db = new DatabaseSync(file); try {db.exec(sql);} finally {db.close();}}
const close = (a: number, b: number) => assert(Math.abs(a - b) < 1e-8, `${a} != ${b}`);

test("real Python broker/telemetry fixture reconciles account contributions with immediate fee expense", async t => {
  source(); t.mock.method(Date, "now", () => Date.parse(fixture.valuations.fresh.updatedAt) + 10);
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  const {portfolio, paperContribution: state} = readPortfolioSnapshot();
  assert.equal(state.status, "available", state.detail);
  const r = state.report!;
  close(r.totals.realizedGrossPnl, 50.1); close(r.totals.cashFees, 1.30374);
  close(r.totals.realizedAfterFeesPnl, 48.79626); close(r.totals.unrealizedPnl!, 49.6);
  close(r.totals.netPnl!, 98.39626); close(r.totals.beforeModeledCostsPnl!, 114);
  close(r.totals.spread!, 13); close(r.totals.slippage!, 1.3);
  close(r.totals.netPnl!, portfolio.totalEquity - portfolio.startingCapital!);
  close(r.totals.realizedAfterFeesPnl, portfolio.realizedPnl); close(r.reconciliationDifference!, 0);
  const closed = r.rows.find(row => row.symbol === "INFY")!;
  assert.equal(closed.quantity, 0); assert.equal(closed.markState, "closed"); close(closed.netPnl!, -12.28011);
  const open = r.rows.find(row => row.symbol === "NIFTY")!;
  close(open.realizedAfterFeesPnl, -.08088); close(open.netPnl!, -1.96088);
  assert.equal(r.ledgerId, 6); assert.match(r.sourceSha256, /^[a-f0-9]{64}$/);
});

test("stale marks and an aged snapshot withhold current totals while retaining known closed results", async t => {
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  for (const name of ["stale", "partial", "fresh"]) {
    source(name);
    t.mock.method(Date, "now", () => Date.parse(fixture.valuations[name].updatedAt) + (name === "fresh" ? 31000 : 10));
    const state = readPortfolioSnapshot().paperContribution;
    assert.equal(state.status, "incomplete", state.detail);
    assert.equal(state.report!.totals.netPnl, null); assert.equal(state.report!.reconciliationDifference, null);
    assert.equal(state.report!.totals.returnContribution, null); close(state.report!.totals.cashFees, 1.30374);
    close(state.report!.rows.find(row => row.symbol === "INFY")!.netPnl!, -12.28011);
    const tcs = state.report!.rows.find(row => row.symbol === "TCS")!;
    assert.equal(tcs.markState, name === "partial" ? "fresh" : "unavailable");
  }
});

test("missing drag and unknown noncash costs remain incomplete; invalid accounting cannot stay current", async t => {
  t.mock.method(Date, "now", () => Date.parse(fixture.valuations.fresh.updatedAt) + 10);
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  source(); mutate("DELETE FROM paper_cost_ledger WHERE id=(SELECT MIN(id) FROM paper_cost_ledger WHERE code='SPREAD')");
  let result = readPortfolioSnapshot();
  assert.equal(result.paperContribution.status, "incomplete");
  assert.equal(result.paperContribution.report!.costCoverage.missingSpreadOrders, 1);
  assert.equal(result.paperContribution.report!.totals.spread, null);
  assert.equal(result.paperContribution.report!.totals.beforeModeledCostsPnl, null);
  close(result.paperContribution.report!.totals.netPnl!, 98.39626);
  source(); mutate("UPDATE paper_cost_ledger SET code='UNKNOWN_NONCASH' WHERE id=1");
  assert.equal(readPortfolioSnapshot().paperContribution.report!.costCoverage.unknownNoncashRows, 1);
  for (const sql of [
    "UPDATE paper_accounts SET cash_balance='10000'",
    "UPDATE paper_positions SET average_price='1' WHERE symbol='TCS'",
    "DELETE FROM paper_positions WHERE symbol='TCS'",
    "UPDATE paper_ledger SET side='SELL' WHERE id=1",
    "UPDATE paper_ledger SET notional='1' WHERE id=1",
    "UPDATE paper_ledger SET market='USA' WHERE id=1",
    "UPDATE paper_ledger SET status='CANCELLED' WHERE id=1",
    "UPDATE paper_cost_ledger SET order_id='orphan' WHERE id=1",
    "UPDATE paper_cost_ledger SET amount='-1' WHERE id=1",
    "UPDATE paper_cost_ledger SET amount='NaN' WHERE id=1",
    "UPDATE paper_cost_ledger SET cash_debit=1 WHERE code='SPREAD'",
    "UPDATE paper_cost_ledger SET code='SPREAD' WHERE id=2",
    "DELETE FROM paper_cost_ledger WHERE cash_debit=1 AND id=(SELECT MIN(id) FROM paper_cost_ledger WHERE cash_debit=1)",
    "UPDATE paper_cost_ledger SET created_at='1999-01-01T00:00:00Z' WHERE id=1",
  ]) {
    source(); mutate(sql); result = readPortfolioSnapshot();
    assert.equal(result.paperContribution.status, "invalid", sql + ': ' + result.paperContribution.detail);
    assert.equal(result.paperContribution.report, null); assert.equal(result.portfolio.status, "invalid");
  }
});

test("snapshot and attribution stay coherent when another WAL writer commits between reads", async t => {
  source(); t.mock.method(Date, "now", () => Date.parse(fixture.valuations.fresh.updatedAt) + 10);
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  const writer = new DatabaseSync(file), original = DatabaseSync.prototype.prepare;
  let committed = false;
  t.mock.method(DatabaseSync.prototype, "prepare", function(this: DatabaseSync, sql: string) {
    if (!committed && sql === "SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id=?") {
      committed = true;
      writer.exec(`BEGIN;
        INSERT INTO paper_ledger VALUES(7,'new-fill','default','TCS','INDIA','EQUITY','BUY',1,'100','100','FILLED','2000-01-01T04:00:59Z',NULL,NULL,NULL,NULL);
        INSERT INTO paper_cost_ledger VALUES(100,'new-fill','default','SPREAD','0',0,'2000-01-01T04:00:59Z');
        INSERT INTO paper_cost_ledger VALUES(101,'new-fill','default','SLIPPAGE','0',0,'2000-01-01T04:00:59Z');
        UPDATE paper_accounts SET cash_balance='9544.39626';
        UPDATE paper_positions SET quantity=4,average_price='105.88' WHERE symbol='TCS'; COMMIT;`);
    }
    return original.call(this, sql);
  });
  try {
    const first = readPortfolioSnapshot();
    assert(committed); assert.equal(first.paperContribution.status, "available", first.paperContribution.detail);
    assert.equal(first.paperContribution.report!.ledgerId, 6); close(first.portfolio.cash, 9644.39626);
    const second = readPortfolioSnapshot();
    assert.equal(second.paperContribution.status, "outdated"); assert.equal(second.paperContribution.report, null);
    assert.equal(second.portfolio.status, "invalid");
  } finally {writer.close();}
});

test("legacy, missing, foreign and corrupted evidence cannot create a current contribution or new store", async t => {
  t.mock.method(Date, "now", () => Date.parse(fixture.valuations.fresh.updatedAt) + 10);
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  source(); mutate("DROP TABLE paper_live_valuations");
  assert.equal(readPortfolioSnapshot().paperContribution.status, "unavailable");
  source(); mutate("DROP TABLE paper_cost_ledger");
  const missingCosts = readPortfolioSnapshot();
  assert.equal(missingCosts.paperContribution.status, "unavailable"); assert.equal(missingCosts.portfolio.status, "invalid");
  for (const patch of [{tenantId: "foreign"}, {currency: "USD"}, {realizedPnl: 999}, {totalEquity: 999}, {holdings: []}, {holdings: null}, {cash: true}]) {
    source(); const db = new DatabaseSync(file);
    db.prepare("UPDATE paper_live_valuations SET payload=?").run(JSON.stringify({...fixture.valuations.fresh, ...patch})); db.close();
    assert.equal(readPortfolioSnapshot().paperContribution.status, "invalid");
  }
  fs.rmSync(file); assert.equal(readPortfolioSnapshot().paperContribution.status, "unavailable"); assert(!fs.existsSync(file));
});

test("private export and persisted Atlas context carry the same incomplete account contribution", async t => {
  source("partial"); t.mock.method(Date, "now", () => Date.parse(fixture.valuations.partial.updatedAt) + 10);
  const {GET} = await import("../app/api/portfolio/contribution/route");
  const response = await GET(); assert.equal(response.status, 200); assert.match(response.headers.get("cache-control")!, /no-store/);
  assert.match(response.headers.get("content-disposition")!, /attachment/);
  const exported = await response.json();
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network-key";
  const {generateAnswer, conversations} = await import("../lib/copilot");
  let supplied = "";
  try {
    const result = await generateAnswer("Explain recorded account contribution", undefined, "paper-contribution-fixture", async (_url, init) => {
      supplied = JSON.parse(String(init?.body)).system;
      return Response.json({content: [{type: "text", text: "Synthetic answer"}], usage: {input_tokens: 10, output_tokens: 2}});
    });
    assert.equal(result.status, "complete");
    const context = JSON.parse(conversations(result.id)[0].context!);
    const a = context.paperContribution.report;
    assert.equal(context.paperContribution.status, "incomplete");
    assert.equal(a.sourceSha256, exported.report.sourceSha256); assert.deepEqual(a.totals, exported.report.totals);
    assert.equal(a.totals.netPnl, null); assert.equal(a.rows.length, 3); assert.equal(a.omittedRows, 0);
    assert.equal(a.rows[0].symbol, "NIFTY"); assert.equal(a.unavailableRows, 1);
    assert.equal(a.fills, undefined); assert.equal(a.costs, undefined); assert(supplied.includes(JSON.stringify(context)));
  } finally {delete process.env.ANTHROPIC_API_KEY;}
  mutate("UPDATE paper_accounts SET cash_balance='0'"); assert.equal((await GET()).status, 503);
});
