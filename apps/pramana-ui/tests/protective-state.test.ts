import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import type {ProtectionSweep, Runtime} from "../lib/pilot";
import {protectionAlert, protectionSweepCheck} from "../lib/protection-sweep";
import {gateTally, riskGateViews} from "../lib/risk-gates";

process.env.PRAMANA_TENANT_ID = "default";
const folder = fs.mkdtempSync(path.join(os.tmpdir(), "gate-refusals-"));
process.env.PRAMANA_LEDGER_PATH = path.join(folder, "ledger.sqlite");
after(() => fs.rmSync(folder, {recursive: true, force: true}));

const now = Date.parse("2026-09-15T06:00:00Z");
const stamp = new Date(now).toISOString();

const sweep = (changes: Partial<ProtectionSweep> = {}): ProtectionSweep => ({
  schema: "pramana.protection_sweep.v1", tenantId: "pilot", checkedAt: stamp, sweptAt: stamp,
  unprotected: [], rebased: [], haltAfterSeconds: 120,
  gapMonitor: {armed: false, unresolved: []}, ...changes,
});
const runtime = (changes: Partial<Runtime> = {}): Runtime =>
  ({status: "running", mode: "paper", halted: false, protectionSweep: sweep(), ...changes});

test("a swept-and-clean book is not published as the same thing as a book nobody swept", () => {
  const clean = protectionAlert(runtime(), "pilot", now);
  assert.equal(clean.severity, "clear");
  assert.match(clean.detail, /Swept 2026-09-15T06:00:00/);
  assert.equal(protectionSweepCheck(runtime(), "pilot", now).pass, true);

  // Identical empty lists, opposite meanings. This is the distinction the block exists
  // for and the one a page must never collapse.
  const unswept = runtime({protectionSweep: sweep({sweptAt: null})});
  assert.equal(protectionAlert(unswept, "pilot", now).severity, "unverified");
  assert.match(protectionAlert(unswept, "pilot", now).detail, /has not completed a protective sweep/);
  assert.equal(protectionSweepCheck(unswept, "pilot", now).pass, false);
});

test("an unpriceable stop reads as exposure, names the symbol and states the halt clock", () => {
  const state = runtime({protectionSweep: sweep({
    unprotected: [{symbol: "INFY", unpricedSince: stamp}], haltAfterSeconds: 120,
  })});
  const alert = protectionAlert(state, "pilot", now);
  assert.equal(alert.severity, "exposed");
  assert.deepEqual(alert.symbols, ["INFY"]);
  assert.match(alert.headline, /1 position carrying an unenforced stop/);
  assert.match(alert.detail, /INFY could not be priced/);
  assert.match(alert.detail, /unpriced since 2026-09-15T06:00:00/);
  assert.match(alert.detail, /entries halt after 120s/);
  assert.equal(protectionSweepCheck(state, "pilot", now).pass, false);
});

test("a re-based quote is reported as a suspended stop rather than as a quiet one", () => {
  const state = runtime({protectionSweep: sweep({rebased: ["INFY", "TCS"]})});
  const alert = protectionAlert(state, "pilot", now);
  assert.equal(alert.severity, "exposed");
  assert.deepEqual(alert.symbols, ["INFY", "TCS"]);
  assert.match(alert.headline, /2 positions carrying an unenforced stop/);
  assert.match(alert.detail, /re-based by a corporate action/);
  // The position is not liquidated and not halted; saying so is the point of the line.
  assert.match(alert.detail, /still held/);
});

test("an unresolved overnight gap is exposure even when every stop could be priced", () => {
  const state = runtime({protectionSweep: sweep({gapMonitor: {armed: true, unresolved: [{
    symbol: "INFY", verdict: "UNDETERMINED", venue: "INDIA", previousMark: "100",
    currentMark: "70", stepFraction: "0.3", nearestAction: "", firstSeenAt: stamp,
    lastAlertAt: stamp, haltsAt: stamp,
  }]}})});
  const alert = protectionAlert(state, "pilot", now);
  assert.equal(alert.severity, "exposed");
  assert.deepEqual(alert.symbols, ["INFY"]);
  assert.match(alert.detail, /larger than the exchange band/);
  assert.match(protectionSweepCheck(state, "pilot", now).detail, /Overnight gap monitor armed/);
});

test("stale, foreign, malformed or halted-engine sweep evidence never claims enforcement", () => {
  for (const changes of [
    {checkedAt: "invalid"}, {checkedAt: new Date(now - 10001).toISOString()},
    {checkedAt: new Date(now + 5001).toISOString()}, {tenantId: "other"}, {schema: "unknown"},
  ]) {
    const state = runtime({protectionSweep: sweep(changes)});
    assert.equal(protectionAlert(state, "pilot", now).severity, "unverified", JSON.stringify(changes));
    assert.equal(protectionSweepCheck(state, "pilot", now).pass, false);
  }
  for (const changes of [{status: "stale"}, {mode: "live"}]) {
    assert.equal(protectionAlert(runtime(changes), "pilot", now).severity, "unverified");
  }
  // A payload with no sweep block at all, and one whose arrays are the wrong shape.
  for (const value of [undefined, null, {schema: "pramana.protection_sweep.v1"} as unknown as ProtectionSweep]) {
    const state = runtime({protectionSweep: value as ProtectionSweep});
    const alert = protectionAlert(state, "pilot", now);
    assert.equal(alert.severity, "unverified");
    assert.match(alert.detail, /not a statement that they can|no protection-sweep state/);
  }
});

test("an unarmed gate reads as off and never as nothing to report", () => {
  const state = runtime({riskGates: {
    schema: "pramana.risk_gates.v1", tenantId: "pilot", checkedAt: stamp, gates: [
      {id: "sector_concentration", setting: "PRAMANA_SECTOR_MAP_JSON", armed: false, records: 0, groups: 0, limit: 0.25},
      {id: "correlation_adjusted_gross", setting: "PRAMANA_BOOK_RISK_HISTORY", armed: true, limit: 0.45},
      {id: "overnight_exposure", setting: "PRAMANA_OVERNIGHT_GROSS_CAP", armed: true, limit: 0.25,
       closingWindowSeconds: 600, observed: 0.1, observedUnavailable: null},
    ],
  }});
  const views = riskGateViews(state, "pilot", now);
  assert.deepEqual(gateTally(views), {armed: 2, unarmed: 1, unverified: 0, total: 3});
  const [group, correlation, overnight] = views;
  assert.equal(group.state, "unarmed");
  assert.match(group.source, /Not armed\. This control is off, which is not the same as having nothing to report/);
  assert.match(group.source, /PRAMANA_SECTOR_MAP_JSON/);
  // An unarmed gate publishes no measurement, so none is shown for it.
  assert.equal(group.measure, "");
  assert.equal(correlation.state, "armed");
  assert.match(correlation.limits, /45\.00% of equity/);
  assert.equal(overnight.measure, "Observed 10.00% of equity against 25.00%.");
  assert.match(overnight.limits, /final 10 minutes of the session/);
});

test("an unavailable measure names its cause instead of showing a number that reads as headroom", () => {
  const state = runtime({riskGates: {
    schema: "pramana.risk_gates.v1", tenantId: "pilot", checkedAt: stamp, gates: [
      {id: "overnight_exposure", setting: "PRAMANA_OVERNIGHT_GROSS_CAP", armed: true, limit: 0.25,
       closingWindowSeconds: 600, observed: null, observedUnavailable: "invalid_position_average"},
    ],
  }});
  const [overnight] = riskGateViews(state, "pilot", now);
  assert.match(overnight.measure, /Unavailable: invalid_position_average/);
  assert.doesNotMatch(overnight.measure, /0\.00%|—/);
});

test("stale, foreign or unpublished gate evidence cannot vouch for what is armed", () => {
  const gates = {schema: "pramana.risk_gates.v1", tenantId: "pilot", checkedAt: stamp,
    gates: [{id: "event_blackout", setting: "PRAMANA_EVENT_CALENDAR", armed: true, records: 3,
      blackouts: [{category: "earnings", symbol: "INFY"}]}]};
  const armed = riskGateViews(runtime({riskGates: gates}), "pilot", now);
  assert.equal(armed[0].state, "armed");
  assert.deepEqual(armed[0].active, ["INFY · earnings"]);
  assert.match(armed[0].source, /3 declared records/);
  // Counts read as sentences, not as templates: one group is a group.
  const single = riskGateViews(runtime({riskGates: {...gates, gates: [
    {id: "sector_concentration", setting: "PRAMANA_SECTOR_MAP_JSON", armed: true, records: 1, groups: 1, limit: 0.25}]}}), "pilot", now);
  assert.match(single[0].source, /1 mapped symbol across 1 group\./);
  for (const state of [
    runtime({riskGates: {...gates, checkedAt: new Date(now - 10001).toISOString()}}),
    runtime({riskGates: {...gates, tenantId: "other"}}),
    runtime({riskGates: gates, status: "stale"}),
  ]) {
    const [view] = riskGateViews(state, "pilot", now);
    assert.equal(view.state, "unverified");
    assert.match(view.source, /Arming is unverified/);
    // A blackout claimed by evidence the page cannot date is not shown as in force.
    assert.deepEqual(view.active, []);
  }
  assert.deepEqual(riskGateViews(runtime({riskGates: {...gates, schema: "other"}}), "pilot", now), []);
  assert.deepEqual(riskGateViews(runtime(), "pilot", now), []);
});

test("an expired workspace withdraws the live-enforcement claim with every other engine claim", async () => {
  const {ageWorkspace} = await import("../lib/freshness");
  const base = {
    generatedAt: stamp, tenantId: "pilot",
    runtime: runtime({updatedAt: stamp}), portfolio: {status: "ok", markMode: "engine_live", markDisclaimer: "", holdings: [], updatedAt: stamp},
    market: {}, checks: [{id: "protection_sweep", title: "Live stop enforcement", pass: true, detail: "clean"}],
  } as never;
  const current = ageWorkspace(base, now).checks.find((c) => c.id === "protection_sweep");
  assert.equal(current?.pass, true);
  const expired = ageWorkspace(base, now + 40000).checks.find((c) => c.id === "protection_sweep");
  assert.equal(expired?.pass, false);
  assert.match(expired!.detail, /Workspace evidence expired/);
});

test("journaled refusals are read per decision, and a missing journal says so in those words", async () => {
  const {readGateRefusals} = await import("../lib/gate-refusals");
  const db = new DatabaseSync(process.env.PRAMANA_LEDGER_PATH!);
  try {
    // No journal at all. "None recorded" and "no record" are opposite claims.
    const missing = readGateRefusals(db);
    assert.equal(missing.status, "unavailable");
    assert.match(missing.detail, /not evidence that none occurred/);
    assert.equal(missing.rejected, null);

    db.exec(`CREATE TABLE paper_decision_journal(decision_id TEXT PRIMARY KEY, tenant_id TEXT,
      symbol TEXT, decided_at TEXT, stance TEXT, governance TEXT, reason TEXT)`);
    const empty = readGateRefusals(db);
    assert.equal(empty.status, "available");
    assert.deepEqual([empty.decisions, empty.rejected, empty.recent.length], [0, 0, 0]);

    const insert = db.prepare("INSERT INTO paper_decision_journal VALUES (?,'default',?,?,?,?,?)");
    insert.run("d1", "INFY", "2026-09-15T05:00:00Z", "BUY", "filled", null);
    insert.run("d2", "INFY", "2026-09-15T05:10:00Z", "BUY", "rejected", "sector_concentration_limit:IT_SERVICES");
    insert.run("d3", "TCS", "2026-09-15T05:20:00Z", "BUY", "rejected", "book_risk_measure_unavailable:no_history:TCS");
    insert.run("d4", "TCS", "2026-09-15T05:30:00Z", "BUY", "rejected", "sector_concentration_limit:IT_SERVICES");
    insert.run("d5", "WIPRO", "2026-09-15T05:40:00Z", "BUY", "rejected", null);
    // Another tenant's refusals are never folded into this account's record.
    db.prepare("INSERT INTO paper_decision_journal VALUES ('d6','other','SBIN','2026-09-15T05:45:00Z','BUY','rejected','event_blackout:earnings')").run();

    const state = readGateRefusals(db);
    assert.equal(state.status, "available");
    assert.deepEqual([state.decisions, state.rejected], [5, 4]);
    assert.deepEqual(state.reasons.map((r) => [r.reason, r.count]), [
      ["sector_concentration_limit:IT_SERVICES", 2],
      ["book_risk_measure_unavailable:no_history:TCS", 1],
      ["unstated", 1],
    ]);
    // Newest first, and each refusal carries the symbol it refused.
    assert.deepEqual(state.recent.map((r) => r.symbol), ["WIPRO", "TCS", "TCS", "INFY"]);
    assert.equal(state.recent[0].reason, "unstated");
    assert.equal(state.scannedSince, "2026-09-15T05:10:00Z");
  } finally {
    db.close();
  }
});
