import assert from "node:assert/strict";
import test from "node:test";
import {DatabaseSync} from "node:sqlite";
import {createElement} from "react";
import {renderToStaticMarkup} from "react-dom/server";
import {ProbeBudget} from "../components/protective-state";
import type {Runtime} from "../lib/pilot";
import {istSession, probeBudgetView, type Exploration, type ProbesToday} from "../lib/probe-budget-model";

// Read by lib/db when the reader is first imported, inside the journal test.
process.env.PRAMANA_TENANT_ID = "ghost";

const NOW = Date.parse("2026-09-24T08:00:00Z"); // 13:30 IST
const block = (overrides: Partial<Exploration> = {}): Exploration => ({
  schema: "pramana.exploration.v1", tenantId: "ghost", checkedAt: new Date(NOW - 2000).toISOString(),
  setting: "PRAMANA_EXPLORATION_MAX_PER_DAY", armed: true, maxPerDay: 3,
  minWeightedScore: 0.35, minConfidence: 0.4, notionalFraction: 0.02,
  withheldInRegimes: ["high_volatility", "trending_down"], ...overrides,
});
const runtime = (exploration: Exploration | null, overrides: Record<string, unknown> = {}) =>
  ({status: "running", mode: "paper", exploration, ...overrides}) as unknown as Runtime;
const counted = (count: number): ProbesToday => ({status: "available", sessionDate: "2026-09-24", count, detail: ""});

test("the IST session is the one the engine counts against, in the journal's own timestamp shape", () => {
  assert.deepEqual(istSession(new Date("2026-09-24T08:00:00Z")),
    {date: "2026-09-24", since: "2026-09-23T18:30:00+00:00", until: "2026-09-24T18:30:00+00:00"});
  // 01:30 IST on the 25th is already the next session.
  assert.equal(istSession(new Date("2026-09-24T20:00:00Z")).date, "2026-09-25");
  assert.equal(istSession(new Date("2026-09-24T18:29:59Z")).date, "2026-09-24");
});

test("today's probes are counted from the journal, for this account and this session only", async () => {
  const {readProbesToday} = await import("../lib/probe-budget");
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE paper_decision_journal (decision_id TEXT, tenant_id TEXT, decided_at TEXT, probe INTEGER)");
  const insert = db.prepare("INSERT INTO paper_decision_journal VALUES (?,?,?,?)");
  for (const [id, tenant, at, probe] of [
    ["a", "ghost", "2026-09-23T18:30:00+00:00", 1],        // 00:00 IST: the first instant of the session
    ["b", "ghost", "2026-09-24T05:01:02.123456+00:00", 1], // mid-session, with microseconds
    ["c", "ghost", "2026-09-24T05:02:00+00:00", 0],        // a hold, not a probe
    ["d", "ghost", "2026-09-23T18:29:59.999999+00:00", 1], // yesterday's last instant
    ["e", "ghost", "2026-09-24T18:30:00+00:00", 1],        // tomorrow's first instant
    ["f", "other", "2026-09-24T05:03:00+00:00", 1],        // another account
  ] as const) insert.run(id, tenant, at, probe);
  assert.deepEqual(readProbesToday(db, new Date(NOW)),
    {status: "available", sessionDate: "2026-09-24", count: 2, detail: "Journaled probes for today's IST session."});

  const empty = new DatabaseSync(":memory:");
  assert.equal(readProbesToday(empty, new Date(NOW)).status, "unavailable");
  empty.exec("CREATE TABLE paper_decision_journal (decision_id TEXT, tenant_id TEXT, decided_at TEXT)");
  const legacy = readProbesToday(empty, new Date(NOW));
  assert.equal(legacy.count, null);
  assert.match(legacy.detail, /predates probe labelling/);
});

test("a cap of 0 says off, names the setting, and never shows a count", () => {
  const view = probeBudgetView(runtime(block({armed: false, maxPerDay: 0})), "ghost", counted(0), NOW);
  assert.equal(view.state, "off");
  assert.equal(view.title, "Probes off: the cap is 0");
  assert.equal(view.used, null);
  assert.match(view.lines[0], /^PRAMANA_EXPLORATION_MAX_PER_DAY is 0, so no hold is turned into a probe/);
  assert.match(view.lines.at(-1)!, /hard hold .* is never probed; it stays a hold/);
});

test("a live budget states its bar, its size, where it never probes, and what is used today", () => {
  const view = probeBudgetView(runtime(block()), "ghost", counted(2), NOW);
  assert.equal(view.state, "on");
  assert.equal(view.title, "Up to 3 probes a day");
  assert.equal(view.used, "2 of 3 used today (2026-09-24 IST)");
  assert.match(view.lines[0], /weighted score of at least 0\.35, with at least 0\.40 average confidence, may enter as a probe sized at 2\.00% of equity\. Full entries are unchanged\./);
  assert.equal(view.lines[1], "Never in high volatility or trending down regimes.");
  assert.match(view.lines.at(-1)!, /never probed; it stays a hold/);

  const unread = probeBudgetView(runtime(block()), "ghost", {status: "unavailable", sessionDate: "2026-09-24", count: null, detail: "The decision journal could not be read."}, NOW);
  assert.equal(unread.used, "today's count unavailable: The decision journal could not be read.");
  assert.equal(probeBudgetView(runtime(block({withheldInRegimes: []})), "ghost", counted(0), NOW).lines[1],
    "No regime withholds probes (regime routing is off).");
});

test("a stale, foreign, contradictory or missing budget is unverified, never off or on", () => {
  for (const [label, rt] of [
    ["missing", runtime(null)],
    ["stale", runtime(block({checkedAt: new Date(NOW - 60_000).toISOString()}))],
    ["foreign", runtime(block({tenantId: "someone-else"}))],
    ["armed with a cap of 0", runtime(block({armed: true, maxPerDay: 0}))],
    ["off with a cap", runtime(block({armed: false, maxPerDay: 3}))],
    ["negative cap", runtime(block({armed: false, maxPerDay: -1}))],
    ["other schema", runtime(block({schema: "pramana.exploration.v2"}))],
    ["engine stopped", runtime(block(), {status: "stopped"})],
    ["not paper", runtime(block(), {mode: "live"})],
  ] as const) {
    const view = probeBudgetView(rt, "ghost", counted(1), NOW);
    assert.equal(view.state, "unverified", label);
    assert.equal(view.used, null, label);
  }
});

test("the panel leads with the state in words", () => {
  const off = renderToStaticMarkup(createElement(ProbeBudget, {runtime: runtime(block({armed: false, maxPerDay: 0, checkedAt: new Date().toISOString()})), tenant: "ghost", probes: counted(0)}));
  assert.match(off, /<h2>Probes off: the cap is 0<\/h2><\/div><span class="pill neutral">Off<\/span>/);
  assert.doesNotMatch(off, /used today/);
  const on = renderToStaticMarkup(createElement(ProbeBudget, {runtime: runtime(block({checkedAt: new Date().toISOString()})), tenant: "ghost", probes: counted(1)}));
  assert.match(on, /<span class="pill green">On<\/span>/);
  assert.match(on, /<strong>1 of 3 used today/);
});
