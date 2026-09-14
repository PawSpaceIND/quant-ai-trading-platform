import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";

const folder = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-valuation-gaps-"));
const file = path.join(folder, "ledger.sqlite");
process.env.PRAMANA_TENANT_ID = "pilot";
process.env.PRAMANA_LEDGER_PATH = file;
after(() => fs.rmSync(folder, {recursive: true, force: true}));

function database() {
  fs.rmSync(file, {force: true});
  const db = new DatabaseSync(file);
  db.exec(`CREATE TABLE paper_ledger(id INTEGER,tenant_id TEXT);
    INSERT INTO paper_ledger VALUES(1,'pilot');
    CREATE TABLE paper_live_valuations(tenant_id TEXT,timestamp TEXT,ledger_id INTEGER,payload TEXT,PRIMARY KEY(tenant_id,timestamp));`);
  return db;
}
const sha = "a".repeat(64);
function sample(at: number, status = "ok") {
  return {status, tenantId: "pilot", currency: "INR", markMode: "engine_live", updatedAt: new Date(at).toISOString(),
    markDisclaimer: status === "invalid" ? "Portfolio totals withheld: invalid_position_quantity. Stored records require review." : "Synthetic engine valuation",
    cash: status === "invalid" ? null : 900, totalEquity: status === "invalid" ? null : 1000,
    startingCapital: status === "invalid" ? null : 1000, holdings: [],
    allMarksFresh: status !== "invalid", qualifyingSession: status !== "invalid",
    strategyObservation: {manifestSha256: sha, eligible: status !== "invalid"}, sessionDate: "2000-01-03"};
}
function insert(db: DatabaseSync, row: ReturnType<typeof sample>) {
  db.prepare("INSERT OR REPLACE INTO paper_live_valuations VALUES('pilot',?,1,?)").run(row.updatedAt, JSON.stringify(row));
}

test("invalid engine valuation is withheld even when its observation later becomes stale", async () => {
  const db = database();
  insert(db, sample(Date.now() - 60000, "invalid"));
  db.close();
  const {readLivePortfolio} = await import("../lib/pilot");
  const {readPortfolioSnapshot} = await import("../lib/portfolio");
  assert.equal(readLivePortfolio()?.status, "invalid");
  const result = readPortfolioSnapshot();
  assert.equal(result.portfolio.status, "invalid");
  assert.match(result.portfolio.markDisclaimer, /invalid_position_quantity/);
  assert.equal(result.paperContribution.status, "invalid");
  assert.equal(result.paperContribution.report, null);
});

test("an unavailable interval is a null chart gap rather than zero or a dropped observation", async () => {
  const db = database(), now = Date.now();
  insert(db, sample(now - 120000));
  insert(db, sample(now - 60000, "invalid"));
  insert(db, sample(now));
  db.close();
  const {readLivePortfolio} = await import("../lib/pilot");
  const result = readLivePortfolio()!;
  assert.equal(result.status, "ok");
  assert.deepEqual(result.equityCurve.map(p => p.equity), [1000, null, 1000]);
});

test("a failed minute excludes the whole day from account and strategy qualification despite enough good samples", async () => {
  const db = database(), start = Date.parse("2000-01-03T03:45:00Z");
  for (let i = 0; i < 375; i++) insert(db, sample(start + i * 60000));
  const {performance} = await import("../lib/pilot");
  const {strategyObservationDays} = await import("../lib/strategy-evidence");
  const coverageStart = new Date(start).toISOString();
  assert.equal(performance().days, 1);
  assert.equal(strategyObservationDays(sha, [], coverageStart).days, 1);
  insert(db, sample(start + 100 * 60000, "invalid"));
  db.close();
  assert.equal(performance().days, 0);
  assert.equal(strategyObservationDays(sha, [], coverageStart).days, 0);
});
