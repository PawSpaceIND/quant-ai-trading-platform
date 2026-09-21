import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ALERT_LABELS, ALERT_SCAN_LIMIT, ALERT_TAIL_BYTES, alertLogPath, readAlerts } from "../lib/alerts";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pramana-alerts-test-"));
process.env.PRAMANA_TENANT_ID = "ghost";
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "ledger.sqlite");
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
after(() => fs.rmSync(dir, { recursive: true, force: true }));

const fixture = fs.readFileSync(new URL("./fixtures/engine-alerts.jsonl", import.meta.url).pathname, "utf8");
const logAt = (body: string) => {
  const file = path.join(dir, `alerts-${Math.random().toString(36).slice(2)}.jsonl`);
  fs.writeFileSync(file, body);
  process.env.PRAMANA_ALERT_LOG = file;
  return file;
};

test("the alert log defaults to the shared volume beside the ledger, as the engine writes it", () => {
  delete process.env.PRAMANA_ALERT_LOG;
  assert.equal(alertLogPath(), path.join(dir, "alerts.jsonl"));
  process.env.PRAMANA_ALERT_LOG = path.join(dir, "configured.jsonl");
  assert.equal(alertLogPath(), path.join(dir, "configured.jsonl"));
});

test("a missing log is unavailable, which is not the same claim as no alerts", () => {
  process.env.PRAMANA_ALERT_LOG = path.join(dir, "absent.jsonl");
  const state = readAlerts();
  assert.notEqual(state.status, "available");
  if (state.status === "available") return;
  assert.equal(state.status, "unavailable");
  assert.deepEqual(state.alerts, []);
  assert.match(state.detail, /not the same as no alerts/);
});

test("alerts the engine wrote are read newest first, with another account's dropped", () => {
  // The fixture is produced by the engine's own JsonlFileSink, not written by hand.
  logAt(fixture);
  const state = readAlerts();
  assert.equal(state.status, "available");
  if (state.status !== "available") return;
  assert.deepEqual(state.alerts.map(a => a.rawCode), [
    "KILL_SWITCH_ENGAGED", "OVERNIGHT_GAP_UNEXPLAINED", "CADENCE_TICK_FAILED",
    "SESSION_PLAN_READY", "MACRO_PROVIDER_UNAVAILABLE",
  ]);
  assert(!JSON.stringify(state.alerts).includes("OTHER-TENANT-PRIVATE"), "another account's alert must not be shown");
  const macro = state.alerts.at(-1)!;
  assert.equal(macro.priority, "CRITICAL");
  assert.equal(macro.label, ALERT_LABELS.MACRO_PROVIDER_UNAVAILABLE);
  assert.deepEqual(macro.metadata, { provider: "FRED", reason: "unauthorized" });
  assert.equal(macro.createdAt, "2026-09-21T03:45:00+00:00");
  assert.equal(macro.loggedAt, "2026-09-21T03:45:03+00:00");
});

test("every code the engine declares has a label, so none can render as a raw enum name", () => {
  // The engine is the source of this list; a code added there without a label here would
  // reach the operator as an unrecognised string, which is the state this panel exists to end.
  const source = fs.readFileSync(new URL("../../../src/quant_ai/notifications/trading.py", import.meta.url).pathname, "utf8");
  const block = source.slice(source.indexOf("class TradingAlertCode"), source.indexOf("class AlertPriority"));
  const declared = [...block.matchAll(/^ {4}([A-Z_]+) = "([A-Z_]+)"$/gm)].map(m => m[2]);
  assert(declared.length >= 17, `expected the engine's alert codes, found ${declared.length}`);
  assert.deepEqual(declared.filter(code => !Object.hasOwn(ALERT_LABELS, code)), []);
});

test("a code this build does not declare is shown as unrecognised rather than guessed", () => {
  logAt(JSON.stringify({
    notification_id: "a".repeat(32), tenant_id: "ghost", code: "FUTURE_CODE",
    priority: "CRITICAL", message: "From a newer engine", created_at: "2026-09-21T04:00:00+00:00", metadata: {},
  }) + "\n");
  const state = readAlerts();
  assert.equal(state.status, "available");
  if (state.status !== "available") return;
  assert.equal(state.alerts[0].code, null);
  assert.equal(state.alerts[0].rawCode, "FUTURE_CODE");
  assert.match(state.alerts[0].label, /Unrecognised code FUTURE_CODE/);
});

test("malformed lines, missing fields and unparseable timestamps are dropped, never guessed", () => {
  const good = fixture.trim().split("\n")[0];
  const rows = [
    "not json",
    "[]",
    JSON.stringify({ notification_id: "b".repeat(32), tenant_id: "ghost", code: "KILL_SWITCH_ENGAGED", created_at: "not-a-date" }),
    JSON.stringify({ tenant_id: "ghost", code: "KILL_SWITCH_ENGAGED", created_at: "2026-09-21T04:00:00+00:00" }),
    JSON.stringify({ notification_id: "c".repeat(32), code: "KILL_SWITCH_ENGAGED", created_at: "2026-09-21T04:00:00+00:00" }),
    good,
  ];
  logAt(rows.join("\n") + "\n");
  const state = readAlerts();
  assert.equal(state.status, "available");
  if (state.status !== "available") return;
  assert.equal(state.alerts.length, 1);
  assert.equal(state.alerts[0].rawCode, "MACRO_PROVIDER_UNAVAILABLE");
});

test("an unbounded log is read as a bounded tail and never whole", () => {
  // The engine appends forever on a shared volume. One poll may not read the whole file.
  const line = (index: number) => JSON.stringify({
    notification_id: String(index).padStart(32, "0"), tenant_id: "ghost", code: "CADENCE_BRIEF",
    priority: "INFO", message: "x".repeat(400), created_at: "2026-09-21T04:00:00+00:00", metadata: {},
  });
  const body = Array.from({ length: 4000 }, (_, i) => line(i)).join("\n") + "\n";
  const file = logAt(body);
  assert(fs.statSync(file).size > ALERT_TAIL_BYTES, "the fixture must exceed the tail bound");
  const state = readAlerts();
  assert.equal(state.status, "available");
  if (state.status !== "available") return;
  assert.equal(state.alerts.length, 50);
  assert.equal(state.truncated, true);
  assert(state.scanned <= ALERT_SCAN_LIMIT);
  // Newest first: the last line written is the first one shown.
  assert.equal(state.alerts[0].id, String(3999).padStart(32, "0"));
  // A tail starts mid-line and a partial record is not a record.
  assert(state.alerts.every(a => a.rawCode === "CADENCE_BRIEF"));
});

test("the route returns the state with no-store and never caches a refusal as success", async () => {
  logAt(fixture);
  const { GET } = await import("../app/api/alerts/route");
  const ok = await GET();
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("Cache-Control"), "no-store");
  assert.equal((await ok.json()).alerts.length, 5);

  process.env.PRAMANA_ALERT_LOG = dir; // a directory, not a file
  const refused = await GET();
  assert.equal(refused.status, 503);
  assert.equal(refused.headers.get("Cache-Control"), "no-store");
  assert.equal((await refused.json()).status, "invalid");
});
