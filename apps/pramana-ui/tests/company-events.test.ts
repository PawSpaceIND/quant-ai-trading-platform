import {after, test} from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import {createHash} from "node:crypto";
import {readCompanyEvents, saveCompanyMapping, companyEventsContext, officialEventUrl} from "../lib/company-events";
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "company-events-test-"));
const fixture = JSON.parse(fs.readFileSync(path.resolve("../../tests/fixtures/company_events.json"), "utf8"));
const file = path.join(dir, "events.sqlite");
process.env.PRAMANA_COMPANY_EVENTS_DB = file;
process.env.PRAMANA_TENANT_ID = "default";
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "missing-ledger.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(dir, "missing-market.json");
process.env.PRAMANA_PROOF_DIR = path.join(dir, "missing-proofs");
after(() => fs.rmSync(dir, {recursive: true, force: true}));
function reset(data = fixture) {
  if (fs.existsSync(file)) fs.unlinkSync(file);
  const db = new DatabaseSync(file);
  db.exec("CREATE TABLE event_revisions(digest TEXT PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE feed_captures(id TEXT PRIMARY KEY,body TEXT NOT NULL,raw BLOB); CREATE TABLE symbol_mappings(title TEXT NOT NULL,verified_at TEXT NOT NULL,symbol TEXT NOT NULL,provenance TEXT NOT NULL,PRIMARY KEY(title,verified_at));");
  for (const row of data.event_revisions) db.prepare("INSERT INTO event_revisions VALUES (?,?)").run(...row);
  for (const [id, body, raw] of data.feed_captures) db.prepare("INSERT INTO feed_captures VALUES (?,?,?)").run(id, body, raw ? Buffer.from(raw, "base64") : null);
  for (const row of data.symbol_mappings) db.prepare("INSERT INTO symbol_mappings VALUES (?,?,?,?)").run(...row);
  db.close();
}
test("dashboard and Python agree on as-of eligibility, newer corrections and ambiguous revisions", () => {
  reset(); const before = fs.readFileSync(file);
  for (const [at, expected] of Object.entries(fixture.pythonAsOfSources)) {
    const state = readCompanyEvents(at); assert.equal(state.status, "available", state.detail);
    const actual = state.events.filter(e => e.mapping?.symbol === "NSE:INFY" && !e.ambiguous).map(e => `nse-event:${e.id}`).sort();
    assert.deepEqual(actual, (expected as {id: string}[]).map(e => e.id).sort());
  }
  const current = readCompanyEvents(); assert.equal(current.latestCapture?.status, "error");
  assert.equal(current.latestCapture?.errorType, "ReadTimeout"); assert.equal(current.captureStale, true);
  assert(current.events.some(e => e.description.includes("தமிழ்")));
  assert(!JSON.stringify(current).includes("PRIVATE_EXCEPTION_SENTINEL"));
  assert(!Object.hasOwn(current.captures[0], "raw")); assert.deepEqual(fs.readFileSync(file), before);
});
test("mapping records append with server time, require a reviewed listed symbol and reject stale requests", () => {
  reset(); const body = {title: "Renamed Limited", symbol: "NSE:INFY", provenance: "Reviewed synthetic instrument master row", expectedVerifiedAt: null, reviewConfirmed: true};
  assert.throws(() => saveCompanyMapping({...body, reviewConfirmed: false}, ["NSE:INFY"]));
  assert.throws(() => saveCompanyMapping(body, ["NSE:TCS"]));
  assert.throws(() => saveCompanyMapping({...body, title: "Invented"}, ["NSE:INFY"]));
  const saved = saveCompanyMapping(body, ["NSE:INFY"]); assert(Date.now() - Date.parse(saved.verifiedAt) < 2000);
  assert.throws(() => saveCompanyMapping(body, ["NSE:INFY"]), /Mapping changed/);
  assert.equal(readCompanyEvents("2026-01-01T04:00:35Z").events[0].mapping, null);
  const current = readCompanyEvents(); assert.equal(current.events.find(e => e.guid === "one")?.mapping?.symbol, "NSE:INFY");
  assert.equal(current.mappings.length, 2);
});
test("missing/wrong/corrupt stores fail visibly without initialization and unsafe links are rejected", () => {
  reset(); const db = new DatabaseSync(file); db.prepare("UPDATE feed_captures SET raw=? WHERE raw IS NOT NULL").run(Buffer.from("bad")); db.close();
  assert.equal(readCompanyEvents().status, "invalid");
  fs.unlinkSync(file); assert.equal(readCompanyEvents().status, "invalid"); assert(!fs.existsSync(file));
  delete process.env.PRAMANA_COMPANY_EVENTS_DB; assert.equal(readCompanyEvents().status, "unavailable"); process.env.PRAMANA_COMPANY_EVENTS_DB = file;
  assert(!officialEventUrl("javascript:alert(1)")); assert(!officialEventUrl("https://nsearchives.nseindia.com.evil.test/a")); assert(!officialEventUrl("https://user:pass@nsearchives.nseindia.com/a"));
  assert(officialEventUrl("https://nsearchives.nseindia.com/a.pdf"));
});
test("Atlas persists bounded eligible event context while retaining missing/conflicting coverage", async () => {
  reset(); const expected = companyEventsContext(); assert(expected.unmappedOrAmbiguous > 0); assert.equal(expected.events.length, 1);
  assert.equal(expected.events[0].captureKind, "imported"); assert(!JSON.stringify(expected).includes("SYNTHETIC_REFERENCE"));
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network";
  const {generateAnswer, conversations} = await import("../lib/copilot");
  let system = "";
  const transport: typeof fetch = async (_url, options) => {system = JSON.parse(String(options?.body)).system; return Response.json({content: [{type: "text", text: "Synthetic answer"}]});};
  const response = await generateAnswer("Explain these disclosures", undefined, "event-context-test", transport);
  assert.equal(response.status, "complete"); const saved = JSON.parse(conversations(response.id)[0].context!);
  assert.deepEqual(saved.companyEvents.events, expected.events); assert(system.includes(JSON.stringify(saved)));
  assert(system.includes("untrusted disclosures")); assert.equal(saved.companyEvents.latestCapture.status, "error");
});

test("long escaped announcement content produces a bounded valid-JSON Atlas handoff", async () => {
  reset(); const {companyEventQuestion} = await import("../lib/company-event-prompt");
  const event = readCompanyEvents().events[0];
  const question = companyEventQuestion({...event, title: '"'.repeat(1000), description: '"'.repeat(30000), sourceUrl: 'https://nsearchives.nseindia.com/' + '"'.repeat(750)}, new Date().toISOString());
  assert(question.length < 3000);
  const data = JSON.parse(question.slice(question.indexOf('{')));
  assert.equal(data.id, event.id); assert.equal(data.captureKind, "imported");
  assert.equal(data.descriptionTruncated, true); assert.equal(data.titleTruncated, true);
});

test("cutoff-scoped Atlas requests exclude later records, current workspace and prior chat", async () => {
  reset(); const {generateAnswer, conversations} = await import("../lib/copilot");
  let sent: {system: string; messages: unknown[]} | undefined;
  const transport: typeof fetch = async (_url, options) => {sent = JSON.parse(String(options?.body)); return Response.json({content: [{type: "text", text: "Historical synthetic answer"}]});};
  const result = await generateAnswer("Explain only the known disclosure", "event-context-test", "historical-event-context-test", transport, "2026-01-01T04:00:25Z");
  assert.equal(result.status, "complete");
  const context = JSON.parse(conversations(result.id)[0].context!);
  assert.equal(context.mode, "company_disclosure_review"); assert.equal(context.portfolio, undefined); assert.equal(context.market, undefined);
  assert.equal(context.companyEvents.events.length, 1); assert.equal(context.companyEvents.events[0].description, "Corrected disclosure");
  assert.equal(sent!.messages.length, 1); assert(!sent!.system.includes("Unicode")); assert(!sent!.system.includes("Company renamed"));
  assert(sent!.system.includes(JSON.stringify(context)));
  await assert.rejects(generateAnswer("Invalid cutoff", undefined, "invalid-cutoff", transport, "2999-01-01T00:00:00Z"));
});


test("automatic company context omits oversized source URLs with an explicit flag", () => {
  reset(); const db = new DatabaseSync(file);
  const row = db.prepare("SELECT digest,body FROM event_revisions").all().find(r => String(r.body).includes("Unicode"))!;
  const event = JSON.parse(String(row.body)); event.source_url = "https://nsearchives.nseindia.com/" + "x".repeat(20000);
  const original = Object.fromEntries(["description", "guid", "published_at", "source_url", "title"].map(k => [k, event[k]]));
  const canonical = JSON.stringify(original).replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  event.revision_sha256 = createHash("sha256").update(canonical).digest("hex");
  db.prepare("UPDATE event_revisions SET digest=?,body=? WHERE digest=?").run(event.revision_sha256, JSON.stringify(event), row.digest); db.close();
  const context = companyEventsContext(); assert.equal(context.status, "available");
  assert.equal(context.events.length, 1); assert.equal(context.events[0].sourceUrl, null); assert.equal(context.events[0].sourceUrlOmitted, true);
  assert(JSON.stringify(context).length < 10000);
});


test("withdrawals append at server time, need the latest review and permit a later corrected mapping", t => {
  t.mock.timers.enable({apis: ["Date"], now: new Date("2026-02-01T00:00:00Z")});
  reset(); const original = readCompanyEvents().mappings[0];
  const body = {action: "revoke", title: "Infosys Limited", symbol: "", provenance: "Reviewed wrong instrument link", expectedVerifiedAt: original.verifiedAt, reviewConfirmed: true};
  assert.throws(() => saveCompanyMapping({...body, reviewConfirmed: false}, []));
  assert.throws(() => saveCompanyMapping({...body, symbol: "NSE:INFY"}, []));
  assert.throws(() => saveCompanyMapping({...body, action: "delete"}, []));
  const result = saveCompanyMapping(body, []); assert.equal(result.status, "revoked"); assert.equal(result.verifiedAt, "2026-02-01T00:00:00.000Z");
  const withdrawn = readCompanyEvents(); assert.deepEqual(withdrawn.mappings[0], original);
  assert.equal(withdrawn.events.find(e => e.guid === "unicode")?.mapping, null);
  assert.equal(withdrawn.events.find(e => e.guid === "unicode")?.latestMapping?.status, "revoked");
  assert.equal(companyEventsContext().includedEvents, 0); assert.equal(companyEventsContext().eventsWithWithdrawnMapping, 2);
  assert.equal(companyEventsContext("2026-01-01T04:01:30Z").includedEvents, 1);
  assert.throws(() => saveCompanyMapping(body, []), /Mapping changed/);
  assert.throws(() => saveCompanyMapping({...body, expectedVerifiedAt: result.verifiedAt}, []));
  t.mock.timers.tick(1);
  const remap = saveCompanyMapping({...body, action: "map", symbol: "NSE:TCS", expectedVerifiedAt: result.verifiedAt}, ["NSE:TCS"]);
  assert.equal(remap.status, "mapped"); assert.equal(companyEventsContext().events[0].symbol, "NSE:TCS");
  assert.equal(companyEventsContext(result.verifiedAt).includedEvents, 0);
  assert.deepEqual(readCompanyEvents().mappings.map(m => m.status), ["mapped", "revoked", "mapped"]);
});

test("Python and dashboard agree on withdrawal, remapping, offset conflicts and microsecond cutoffs", () => {
  const lifecycle = JSON.parse(fs.readFileSync(path.resolve("../../tests/fixtures/company_mapping_lifecycle.json"), "utf8"));
  reset(lifecycle);
  for (const example of lifecycle.lifecycleSources) {
    const state = readCompanyEvents(example.at); assert.equal(state.status, "available", example.at);
    const actual = state.events.filter(e => e.mapping?.symbol === example.symbol && !e.ambiguous).map(e => ({id: `nse-event:${e.id}`, available_at: e.availableAt})).sort((a,b) => a.id.localeCompare(b.id));
    assert.deepEqual(actual, example.sources.map((e: {id: string; available_at: string}) => ({id: e.id, available_at: e.available_at})).sort((a: {id: string},b: {id: string}) => a.id.localeCompare(b.id)), `${example.at} ${example.symbol}`);
  }
  const conflict = readCompanyEvents("2026-01-01T04:00:13Z").events[0]; assert.equal(conflict.mappingAmbiguous, true); assert.equal(conflict.mapping, null);
  assert.equal(readCompanyEvents("2026-01-01T04:00:14Z").events[0].mappingAmbiguous, false);
  assert.equal(readCompanyEvents("2026-01-01T04:00:15.000199Z").events.find(e => e.guid === "micro")?.description, "Microsecond original");
  assert.equal(readCompanyEvents("2026-01-01T04:00:15.000200Z").events.find(e => e.guid === "micro")?.description, "Microsecond correction");
});

test("withdrawn automatic Atlas evidence stays excluded and manual handoff identifies its withdrawn state", async () => {
  reset(); const original = readCompanyEvents().mappings[0];
  const withdrawn = saveCompanyMapping({action: "revoke", title: original.title, symbol: "", provenance: "Wrong symbol", expectedVerifiedAt: original.verifiedAt, reviewConfirmed: true}, []);
  const {companyEventQuestion} = await import("../lib/company-event-prompt");
  const event = readCompanyEvents().events.find(e => e.guid === "unicode")!;
  const prompt = companyEventQuestion(event, withdrawn.verifiedAt);
  const data = JSON.parse(prompt.slice(prompt.indexOf("{"))); assert.equal(data.mappingState, "revoked"); assert.equal(data.symbol, null);
  const {generateAnswer, conversations} = await import("../lib/copilot");
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network";
  let sent = "";
  const transport: typeof fetch = async (_url, options) => {sent = JSON.parse(String(options?.body)).system; return Response.json({content: [{type: "text", text: "Withdrawn synthetic mapping"}]});};
  const result = await generateAnswer("Explain evidence coverage", undefined, "withdrawn-event-context-test", transport, withdrawn.verifiedAt);
  assert.equal(result.status, "complete");
  const context = JSON.parse(conversations(result.id)[0].context!);
  assert.equal(context.companyEvents.events.length, 0); assert.equal(context.companyEvents.eventsWithWithdrawnMapping, 2);
  assert(!sent.includes("Unicode")); assert(sent.includes("Withdrawn or conflicting mappings")); assert(sent.includes(JSON.stringify(context)));
});
