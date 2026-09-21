import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  LESSON_LIMIT,
  POST_MORTEM_FILE_LIMIT,
  hourLabel,
  money,
  parseDecisionQuality,
  parseMissedOpportunities,
  percent,
  ratio,
  readDecisionQuality,
  readMissedOpportunities,
  readPostMortems,
  signedMoney,
  signedPercent,
  stancesSummary,
  truncateLesson,
  verdictFor,
} from "../lib/decision-quality";
import type { DecisionQualityReport } from "../lib/decision-quality";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "decision-quality-test-"));
process.env.PRAMANA_TENANT_ID = "ghost";
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "ledger.sqlite");
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
delete process.env.PRAMANA_DECISION_QUALITY_REPORT;
delete process.env.PRAMANA_POST_MORTEM_DIR;
delete process.env.PRAMANA_MISSED_OPPORTUNITY_DIR;
after(() => fs.rmSync(dir, { recursive: true, force: true }));

const fixturePath = new URL("./fixtures/decision-quality.json", import.meta.url).pathname;
const postMortemPath = new URL("./fixtures/post-mortem.json", import.meta.url).pathname;
const fixture = () => JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const postMortem = () => JSON.parse(fs.readFileSync(postMortemPath, "utf8"));
const reportFile = path.join(dir, "decision-quality.json");
const mortemDir = path.join(dir, "post-mortems");
const writeReport = (body: unknown) => fs.writeFileSync(reportFile, JSON.stringify(body));
const report = (): DecisionQualityReport => { const parsed = parseDecisionQuality(JSON.stringify(fixture())); assert(parsed); return parsed; };

test("reader returns null when no report has been written", () => {
  assert.equal(fs.existsSync(reportFile), false);
  assert.equal(readDecisionQuality(), null);
  process.env.PRAMANA_DECISION_QUALITY_REPORT = path.join(dir, "elsewhere.json");
  assert.equal(readDecisionQuality(), null);
  delete process.env.PRAMANA_DECISION_QUALITY_REPORT;
});

test("wrong schema, missing sections and non-finite required numbers are rejected", () => {
  assert.equal(parseDecisionQuality("not json"), null);
  assert.equal(parseDecisionQuality("[]"), null);
  const mutations: Array<(r: ReturnType<typeof fixture>) => void> = [
    (r) => { r.schema = "pramana.decision_quality.v0"; },
    (r) => { r.counts.decisions = "61"; },
    (r) => { r.counts.decisions = null; },
    (r) => { delete r.counts.closed_trades; },
    (r) => { r.directional.hit_rate = "0.55"; },
    (r) => { r.trades.net_pnl = undefined; },
    (r) => { r.trades.profit_factor = "inf"; },
    (r) => { r.insufficient_sample = "no"; },
    (r) => { r.window.sessions = "ten"; },
    (r) => { delete r.calibration; },
    (r) => { r.calibration.bins[2].decisions = "1"; },
    (r) => { r.by_regime[0].net_pnl = null; },
    (r) => { r.recent[0].governance = "maybe"; },
    (r) => { r.recent[0].decided_at = "yesterday"; },
    (r) => { r.limitations = "text"; },
    (r) => { r.generated_at = 1726308900; },
  ];
  for (const mutate of mutations) {
    const r = fixture();
    mutate(r);
    writeReport(r);
    assert.equal(readDecisionQuality(), null, `mutation ${mutations.indexOf(mutate)} should be rejected`);
  }
});

test("oversize reports are rejected before parsing", () => {
  writeReport({ ...fixture(), padding: "x".repeat(4 * 1024 * 1024) });
  assert(fs.statSync(reportFile).size > 4 * 1024 * 1024);
  assert.equal(readDecisionQuality(), null);
});

test("a contract-exact fixture is accepted, normalized and ordered newest first", () => {
  writeReport({ ...fixture(), raw_prompts: "PRIVATE INPUT" });
  const parsed = readDecisionQuality();
  assert(parsed);
  assert.equal("raw_prompts" in parsed, false);
  assert.equal(parsed.schema, "pramana.decision_quality.v1");
  assert.equal(parsed.tenant_id, "ghost");
  assert.equal(parsed.window.sessions, 10);
  assert.equal(parsed.counts.decisions, 61);
  assert.equal(parsed.directional.hit_rate, 0.5588);
  assert.equal(parsed.trades.average_loss, -274.9);
  assert.equal(parsed.calibration.bins.length, 10);
  assert.equal(parsed.calibration.bins[0].hit_rate, null);
  assert.deepEqual(parsed.by_hour_ist.map((h) => h.hour), [9, 10, 11, 14]);
  assert.deepEqual(parsed.recent.map((d) => d.decision_id), ["d-0219", "d-0216", "d-0212"]);
  assert.equal(parsed.recent[0].governance, "filled");
  assert.equal(parsed.recent[1].regime, null);
  assert.equal(parsed.limitations.length, 2);
  process.env.PRAMANA_DECISION_QUALITY_REPORT = reportFile;
  assert.equal(readDecisionQuality()?.counts.filled, 28);
  delete process.env.PRAMANA_DECISION_QUALITY_REPORT;
});

test("post-mortems read newest first, skip malformed files and truncate lessons to plain text", () => {
  assert.deepEqual(readPostMortems(), []);
  fs.mkdirSync(mortemDir);
  const write = (name: string, body: unknown) => fs.writeFileSync(path.join(mortemDir, name), typeof body === "string" ? body : JSON.stringify(body));
  write("2026-09-12.json", postMortem());
  const long = `Line one\nline two${String.fromCharCode(7)} ${"detail ".repeat(60)}`;
  write("2026-09-14.json", { ...postMortem(), session_date: "2026-09-14", generated_at: "2026-09-14T10:40:00Z", status: "pending", approved_at: null, summary: {}, lessons: [long, "Short lesson."], evidence: {} });
  write("2026-09-13.json", "{not json");
  write("2026-09-11.json", { ...postMortem(), session_date: "2026-09-11", schema: "pramana.post_mortem.v0" });
  write("2026-09-10.json", { ...postMortem(), session_date: "2026-09-09" });
  write("2026-09-08.json", { ...postMortem(), session_date: "2026-09-08", lessons: ["ok", 42] });
  write("2026-09-06.json", { ...postMortem(), session_date: "2026-09-06", status: "rejected" });
  write("2026-09-05.json", { ...postMortem(), session_date: "2026-09-05", lessons: ["y".repeat(600_000)] });
  write("notes.txt", "ignored");
  write("latest.json", postMortem());
  const items = readPostMortems();
  assert.deepEqual(items.map((p) => p.session_date), ["2026-09-14", "2026-09-12"]);
  assert.equal(items[0].status, "pending");
  assert.equal(items[0].approved_at, null);
  assert.equal(items[0].lessons.length, 2);
  assert.equal(items[0].lessons[0].length, LESSON_LIMIT);
  assert(items[0].lessons[0].endsWith("…"));
  assert.equal(items[0].lessons[0].startsWith("Line one line two detail"), true);
  assert.equal(items[0].lessons[0].includes("\n") || items[0].lessons[0].includes(String.fromCharCode(7)), false);
  assert.equal(items[1].status, "approved");
  assert.equal(items[1].approved_at, "2026-09-12T16:02:00Z");
  assert.deepEqual(items[1].summary, { decisions: 6, filled: 3, net_pnl: -142.5, regime: "RANGE_BOUND", reviewed: true, notes: null });
  assert.equal("evidence" in items[1], false);
  assert.equal(items[1].lessons[1], "Quote-staleness rejections clustered between 09:15 and 09:30 IST.");
});

test("post-mortem reads are capped at the newest files and a missing directory is empty", () => {
  const capped = path.join(dir, "many-post-mortems");
  fs.mkdirSync(capped);
  for (let i = 0; i < 35; i++) {
    const date = new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 10);
    fs.writeFileSync(path.join(capped, `${date}.json`), JSON.stringify({ ...postMortem(), session_date: date }));
  }
  process.env.PRAMANA_POST_MORTEM_DIR = capped;
  const items = readPostMortems();
  assert.equal(items.length, POST_MORTEM_FILE_LIMIT);
  assert.equal(items[0].session_date, "2026-02-04");
  assert.equal(items[items.length - 1].session_date, "2026-01-06");
  process.env.PRAMANA_POST_MORTEM_DIR = path.join(dir, "missing-post-mortems");
  assert.deepEqual(readPostMortems(), []);
  delete process.env.PRAMANA_POST_MORTEM_DIR;
});

test("verdictFor names the rule in every state", () => {
  const edge = verdictFor(report());
  assert.equal(edge.state, "edge_candidate");
  assert.match(edge.sentence, /^Edge candidate: 34 evaluated decisions \(minimum 20\)/);
  assert.match(edge.sentence, /hit rate above 50%, expectancy above 0, profit factor above 1 and Brier score below 0\.25/);
  assert.match(edge.sentence, /not a live-trading approval/);
  assert.deepEqual(edge.checks.map((c) => c.pass), [true, true, true, true]);

  const short = report();
  short.directional.evaluated = 12;
  const insufficient = verdictFor(short);
  assert.equal(insufficient.state, "insufficient_sample");
  assert.match(insufficient.sentence, /^12 of 20 directional decisions evaluated; do not read these numbers as edge yet\./);
  assert.match(insufficient.sentence, /at least 20 decisions have a 60-minute outcome/);

  const flagged = report();
  flagged.insufficient_sample = true;
  const flaggedVerdict = verdictFor(flagged);
  assert.equal(flaggedVerdict.state, "insufficient_sample");
  assert.match(flaggedVerdict.sentence, /^34 of 20 directional decisions evaluated; do not read these numbers as edge yet\./);
  assert.match(flaggedVerdict.sentence, /marks the sample as insufficient/);

  const weak = report();
  weak.trades.profit_factor = 0.9;
  const noEdge = verdictFor(weak);
  assert.equal(noEdge.state, "no_edge_yet");
  assert.match(noEdge.sentence, /^No edge yet: 34 evaluated decisions meet the minimum of 20/);
  assert.match(noEdge.sentence, /Not met: profit factor 0\.90\./);
  assert.deepEqual(noEdge.checks.map((c) => c.pass), [true, true, false, true]);

  const unknown = report();
  unknown.calibration.brier_score = null;
  unknown.directional.hit_rate = 0.5;
  const nullVerdict = verdictFor(unknown);
  assert.equal(nullVerdict.state, "no_edge_yet");
  assert.match(nullVerdict.sentence, /hit rate 60m 50\.00%, brier score unavailable/);
});

test("formatting helpers use the workspace placeholder and en-IN money style", () => {
  assert.equal(percent(null), "—");
  assert.equal(percent(undefined), "—");
  assert.equal(percent(0.5588), "55.88%");
  assert.equal(percent(0.5, 0), "50%");
  assert.equal(signedPercent(0.0012), "+0.12%");
  assert.equal(signedPercent(-0.0021), "-0.21%");
  assert.equal(ratio(1.384), "1.38");
  assert.equal(ratio(null), "—");
  assert.equal(money(1020), "1,020.00");
  assert.equal(money(123456.7), "1,23,456.70");
  assert.equal(money(null), "—");
  assert.equal(signedMoney(42.5), "+42.50");
  assert.equal(signedMoney(-274.9), "-274.90");
  assert.equal(signedMoney(NaN), "—");
  assert.equal(hourLabel(9), "09:00–10:00");
  assert.equal(hourLabel(23), "23:00–00:00");
  assert.equal(truncateLesson(`x${String.fromCharCode(0)}y\n z`), "x y z");
  assert.equal(truncateLesson("a".repeat(250)).length, 200);
  assert.equal(truncateLesson("short"), "short");
});

/* ---------- missed opportunities ---------- */

// The 21 September 2026 shape: every decision a hold, scored on the 60-minute horizon.
const missedFixture = () => ({
  schema: "pramana.missed_opportunities.v1",
  generated_at: "2026-09-21T10:30:00+00:00",
  tenant_id: "ghost",
  session_date: "2026-09-21",
  threshold: 0.01,
  horizon: "forward_return_60m",
  decisions: 13,
  holds: 11,
  evaluated: 10,
  missed: 4,
  avoided: 3,
  unresolved: 1,
  symbols: [
    { symbol: "RELIANCE", missed: 2, avoided: 0, evaluated: 3, best: {
      decision_id: "rel-2", decided_at: "2026-09-21T11:20:00+05:30", reference_price: 2950, forward_return: 0.018,
      regime: "BULL_TRENDING", mode: "llm", reason: null,
      agents: { "technical-quant-mas": { stance: "BUY", confidence: "0.71" }, "indian-equities": { stance: "NEUTRAL", confidence: "0.40" } },
    } },
    { symbol: "TCS", missed: 0, avoided: 2, evaluated: 3, best: null },
  ],
  limitations: ["Measured on the feed's last traded price; costs ignored."],
});
const missedDir = path.join(dir, "missed-opportunities");

test("missed-opportunity files parse fail-closed and the session date must match the file name", () => {
  const parsed = parseMissedOpportunities(JSON.stringify(missedFixture()));
  assert(parsed);
  assert.equal(parsed.missed, 4);
  assert.equal(parsed.symbols[0].best?.forward_return, 0.018);
  assert.equal(parsed.symbols[0].best?.agents["technical-quant-mas"].stance, "BUY");
  assert.equal(parsed.symbols[1].best, null);
  assert.equal(stancesSummary(parsed.symbols[0].best!.agents), "technical-quant-mas BUY, 1 neutral");
  assert.equal(stancesSummary({ a: { stance: "NEUTRAL", confidence: "0.4" } }), "specialists neutral");
  assert.equal(stancesSummary({}), "no specialist votes");
  assert.equal(parseMissedOpportunities("{not json"), null);
  assert.equal(parseMissedOpportunities(JSON.stringify({ ...missedFixture(), schema: "pramana.missed_opportunities.v0" })), null);
  assert.equal(parseMissedOpportunities(JSON.stringify({ ...missedFixture(), missed: "4" })), null);
  assert.equal(parseMissedOpportunities(JSON.stringify(missedFixture()), "2026-09-20"), null);
  const broken = missedFixture();
  broken.symbols[0].best!.forward_return = Number.NaN;
  assert.equal(parseMissedOpportunities(JSON.stringify(broken)), null);
});

test("the newest readable session file wins and the notified markers are ignored", () => {
  assert.equal(readMissedOpportunities(), null);
  fs.mkdirSync(missedDir);
  const write = (name: string, body: unknown) => fs.writeFileSync(path.join(missedDir, name), typeof body === "string" ? body : JSON.stringify(body));
  write("2026-09-21.json", missedFixture());
  write("2026-09-22.json", "{not json");
  write("2026-09-23.json", { ...missedFixture(), session_date: "2026-09-21" });
  write(".notified-2026-09-21", "2026-09-21T10:30:00+00:00");
  assert.equal(readMissedOpportunities()?.session_date, "2026-09-21");
  process.env.PRAMANA_MISSED_OPPORTUNITY_DIR = path.join(dir, "missing-missed");
  assert.equal(readMissedOpportunities(), null);
  delete process.env.PRAMANA_MISSED_OPPORTUNITY_DIR;
});

test("the API route returns report, post-mortems and verdict with no-store", async () => {
  writeReport(fixture());
  const { GET } = await import("../app/api/decision-quality/route");
  const response = await GET();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  const body = await response.json();
  assert.equal(body.report.tenant_id, "ghost");
  assert.equal(body.missed.session_date, "2026-09-21");
  assert.equal(body.verdict.state, "edge_candidate");
  assert.deepEqual(body.postMortems.map((p: { session_date: string }) => p.session_date), ["2026-09-14", "2026-09-12"]);
  assert.equal(body.postMortems[1].evidence, undefined);
  fs.rmSync(reportFile);
  const empty = await (await GET()).json();
  assert.equal(empty.report, null);
  assert.equal(empty.verdict, null);
  assert.equal(empty.postMortems.length, 2);
});

test("an absent or malformed ai_budget never blanks the report", () => {
  const withoutBudget = fixture();
  delete withoutBudget.ai_budget;
  assert.equal(parseDecisionQuality(JSON.stringify(withoutBudget))?.ai_budget, null);

  for (const broken of [null, "exhausted", 42, {}, { day: "2026-09-15" }, { ...budgetBlock(), calls: "12" }]) {
    const report = fixture();
    report.ai_budget = broken;
    const parsed = parseDecisionQuality(JSON.stringify(report));
    assert.ok(parsed, "a malformed budget must not reject the whole report");
    assert.equal(parsed.ai_budget, null);
  }
});

test("a well-formed ai_budget is parsed, including the exhausted flag", () => {
  const report = fixture();
  report.ai_budget = budgetBlock();
  const parsed = parseDecisionQuality(JSON.stringify(report));
  assert.ok(parsed?.ai_budget);
  assert.equal(parsed.ai_budget.calls, 12);
  assert.equal(parsed.ai_budget.daily_call_limit, 500);
  assert.equal(parsed.ai_budget.exhausted, false);

  report.ai_budget = { ...budgetBlock(), calls: 500, remaining_calls: 0, exhausted: true };
  const spent = parseDecisionQuality(JSON.stringify(report));
  assert.equal(spent?.ai_budget?.exhausted, true);
});

function budgetBlock() {
  return {
    day: "2026-09-15", scope: "consensus", calls: 12, tokens: 48000,
    daily_call_limit: 500, daily_token_limit: 2000000,
    remaining_calls: 488, remaining_tokens: 1952000, exhausted: false,
  };
}
