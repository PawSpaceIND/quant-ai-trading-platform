import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  DASH,
  FORECAST_SCORING_SCHEMA,
  LESSON_LIMIT,
  POST_MORTEM_FILE_LIMIT,
  hourLabel,
  money,
  parseDecisionQuality,
  parseMissedOpportunities,
  parsePostMortem,
  percent,
  ratio,
  readDecisionQuality,
  readMissedOpportunities,
  readPostMortems,
  signedMoney,
  signedPercent,
  signedRatio,
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

test("quality, lessons and missed opportunities reject another account's evidence", () => {
  const isolated = fs.mkdtempSync(path.join(os.tmpdir(), "quality-account-boundary-"));
  const keys = ["PRAMANA_TENANT_ID", "PRAMANA_DECISION_QUALITY_REPORT", "PRAMANA_POST_MORTEM_DIR", "PRAMANA_MISSED_OPPORTUNITY_DIR"] as const;
  const previous = keys.map(key => process.env[key]);
  try {
    process.env.PRAMANA_TENANT_ID = "current-account";
    process.env.PRAMANA_DECISION_QUALITY_REPORT = path.join(isolated, "quality.json");
    process.env.PRAMANA_POST_MORTEM_DIR = path.join(isolated, "lessons");
    process.env.PRAMANA_MISSED_OPPORTUNITY_DIR = path.join(isolated, "missed");
    fs.mkdirSync(process.env.PRAMANA_POST_MORTEM_DIR);
    fs.mkdirSync(process.env.PRAMANA_MISSED_OPPORTUNITY_DIR);
    const q = fixture(), p = postMortem(), m = missedFixture();
    const publish = () => {
      fs.writeFileSync(process.env.PRAMANA_DECISION_QUALITY_REPORT!, JSON.stringify(q));
      fs.writeFileSync(path.join(process.env.PRAMANA_POST_MORTEM_DIR!, `${p.session_date}.json`), JSON.stringify(p));
      fs.writeFileSync(path.join(process.env.PRAMANA_MISSED_OPPORTUNITY_DIR!, `${m.session_date}.json`), JSON.stringify(m));
    };
    publish();
    assert.equal(readDecisionQuality(), null);
    assert.deepEqual(readPostMortems(), []);
    assert.equal(readMissedOpportunities(), null);
    q.tenant_id = p.tenant_id = m.tenant_id = "current-account";
    publish();
    assert.equal(readDecisionQuality()?.tenant_id, "current-account");
    assert.equal(readPostMortems()[0]?.tenant_id, "current-account");
    assert.equal(readMissedOpportunities()?.tenant_id, "current-account");
  } finally {
    keys.forEach((key, index) => { if (previous[index] === undefined) delete process.env[key]; else process.env[key] = previous[index]; });
    fs.rmSync(isolated, {recursive: true, force: true});
  }
});

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
  const at = (date: string, extra: Record<string, unknown> = {}) => ({...postMortem(), session_date: date, ...extra});
  write("2026-09-12.json", at("2026-09-12", {status: "approved", approved_at: "2026-09-12T16:02:00Z"}));
  const long = `Line one\nline two${String.fromCharCode(7)} ${"detail ".repeat(60)}`;
  write("2026-09-14.json", at("2026-09-14", {generated_at: "2026-09-14T10:40:00Z", status: "pending", approved_at: null, lessons: [long, "Short lesson."], evidence: {}}));
  write("2026-09-13.json", "{not json");
  write("2026-09-11.json", at("2026-09-11", {schema: "pramana.post_mortem.v0"}));
  write("2026-09-10.json", at("2026-09-09"));
  write("2026-09-08.json", at("2026-09-08", {lessons: ["ok", 42]}));
  write("2026-09-06.json", at("2026-09-06", {status: "rejected"}));
  write("2026-09-05.json", at("2026-09-05", {lessons: ["y".repeat(600_000)]}));
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
  assert.deepEqual(items[1].summary.counts,
    { decisions: 12, filled: 3, rejected: 3, abstained: 6, resolved_60m: 9, closed_trades: 3, probes: 0 });
  assert.equal(items[1].summary.net_pnl, 272);
  assert.equal(items[1].summary.hit_rate_60m, 0.666667);
  assert.deepEqual(items[1].summary.rejections,
    [{ reason: "paper_naked_sell_disabled", count: 2 }, { reason: "pilot_price_moved_during_analysis", count: 1 }]);
  assert.deepEqual(items[1].summary.exits.map((e) => e.trigger), ["session_close", "stop_loss", "take_profit"]);
  assert.deepEqual(items[1].summary.by_regime.map((r) => r.regime), ["ranging", "high_volatility", "trending_up"]);
  assert.equal(items[1].summary.by_regime[1].hit_rate, null);
  assert.equal(items[1].summary.by_hour_ist.length, 7);
  assert.equal("evidence" in items[1], false);
  assert.match(items[1].lessons[0], /Closed trades netted/);
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

/** The fixture's rates over a sample the edge rule may read: 240 evaluated decisions and a
 * promotion report that passed over the same window. */
const earned = (): DecisionQualityReport => {
  const r = report();
  r.directional.evaluated = 240;
  r.promotion = { verdict: "pass", promotion_authorized: true, minimum_resolved: 200, resolved_forecast_count: 240, basis: "weighted_lean_times_confidence.v1", missing_inputs: [] };
  return r;
};

test("verdictFor names the rule in every state", () => {
  const edge = verdictFor(earned());
  assert.equal(edge.state, "edge_candidate");
  assert.match(edge.sentence, /^Edge candidate: 240 evaluated decisions \(minimum 200\)/);
  assert.match(edge.sentence, /hit rate above 50%, expectancy above 0, profit factor above 1 and Brier score below 0\.25/);
  assert.match(edge.sentence, /not a live-trading approval/);
  assert.deepEqual(edge.checks.map((c) => c.pass), [true, true, true, true, true]);

  const short = earned();
  short.directional.evaluated = 12;
  const insufficient = verdictFor(short);
  assert.equal(insufficient.state, "insufficient_sample");
  assert.match(insufficient.sentence, /^12 of 200 directional decisions evaluated; do not read these numbers as edge yet\./);
  assert.match(insufficient.sentence, /at least 200 decisions have a 60-minute outcome and the promotion report passes/);

  const flagged = earned();
  flagged.insufficient_sample = true;
  const flaggedVerdict = verdictFor(flagged);
  assert.equal(flaggedVerdict.state, "insufficient_sample");
  assert.match(flaggedVerdict.sentence, /^240 directional decisions evaluated \(minimum 200\); do not read these numbers as edge yet\./);
  assert.match(flaggedVerdict.sentence, /marks the sample as insufficient/);

  const weak = earned();
  weak.trades.profit_factor = 0.9;
  const noEdge = verdictFor(weak);
  assert.equal(noEdge.state, "no_edge_yet");
  assert.match(noEdge.sentence, /^No edge yet: 240 evaluated decisions meet the minimum of 200/);
  assert.match(noEdge.sentence, /Not met: profit factor 0\.90\./);
  assert.deepEqual(noEdge.checks.map((c) => c.pass), [true, true, false, true, true]);

  const unknown = earned();
  unknown.calibration.brier_score = null;
  unknown.directional.hit_rate = 0.5;
  const nullVerdict = verdictFor(unknown);
  assert.equal(nullVerdict.state, "no_edge_yet");
  assert.match(nullVerdict.sentence, /hit rate 60m 50\.00%, brier score unavailable/);
});

test("rates that clear the page's own rule are not an edge below 200 or without a passing promotion", () => {
  // The fixture clears every part of the rule on 34 decisions. Before the gate that was a
  // green "Edge candidate"; it is now a sample that says how far it has to go.
  const fixtureVerdict = verdictFor(report());
  assert.equal(fixtureVerdict.state, "insufficient_sample");
  assert.match(fixtureVerdict.sentence, /^34 of 200 directional decisions evaluated/);
  assert.equal(fixtureVerdict.minimumSample, 200);

  const one = earned();
  one.directional.evaluated = 199;
  assert.equal(verdictFor(one).state, "insufficient_sample");

  const unreported = earned();
  unreported.promotion = null;
  const silent = verdictFor(unreported);
  assert.equal(silent.state, "insufficient_sample");
  assert.match(silent.sentence, /carries no promotion verdict/);
  assert.equal(silent.checks.at(-1)?.value, "not reported");

  const noDrawdown = earned();
  noDrawdown.promotion = { ...noDrawdown.promotion!, verdict: "missing_inputs", promotion_authorized: false, missing_inputs: ["drawdown_or_policy_unavailable"] };
  const missing = verdictFor(noDrawdown);
  assert.equal(missing.state, "insufficient_sample");
  assert.match(missing.sentence, /promotion report over this window says missing inputs \(missing: drawdown or policy unavailable\)/);
  assert.deepEqual(missing.checks.at(-1), { label: "Promotion", value: "missing inputs", pass: false });

  for (const verdict of ["fail", "basis_mixed", "insufficient_sample"]) {
    const refused = earned();
    refused.promotion = { ...refused.promotion!, verdict, promotion_authorized: false };
    assert.equal(verdictFor(refused).state, "insufficient_sample", verdict);
  }

  // The floor is the promotion report's own: a report stating a lower minimum does not
  // lower it, and one stating a higher minimum raises it.
  const lax = earned();
  lax.minimum_sample = 20;
  lax.promotion = { ...lax.promotion!, minimum_resolved: 50 };
  lax.directional.evaluated = 120;
  assert.equal(verdictFor(lax).state, "insufficient_sample");
  const strict = earned();
  strict.minimum_sample = 300;
  assert.equal(verdictFor(strict).minimumSample, 300);
  assert.equal(verdictFor(strict).state, "insufficient_sample");
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
  // The fixture predates the promotion gate, so its rates are never read as an edge.
  assert.equal(body.verdict.state, "insufficient_sample");
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

test("a no-trade session still explains itself through counts, rejections and regimes", () => {
  // The session of 21 September 2026 produced no fill at all. A post-mortem that carried
  // only net P&L and hit rate would show a blank card for exactly the day that needs one.
  const raw = fs.readFileSync(new URL("./fixtures/post-mortem-no-trade.json", import.meta.url).pathname, "utf8");
  const parsed = parsePostMortem(raw, "2026-09-21");
  assert(parsed);
  assert.equal(parsed.summary.counts.filled, 0);
  assert.equal(parsed.summary.counts.decisions, 12);
  assert.equal(parsed.summary.counts.abstained, 9);
  assert.equal(parsed.summary.net_pnl, 0);
  // No closed trade means no hit rate. Zero would read as "every call was wrong".
  assert.equal(parsed.summary.hit_rate_60m, null);
  assert.deepEqual(parsed.summary.exits, []);
  assert.deepEqual(parsed.summary.rejections, [{ reason: "paper_naked_sell_disabled", count: 3 }]);
  assert(parsed.summary.by_regime.length > 0);
  assert(parsed.summary.by_regime.every((r) => r.filled === 0));
  assert(parsed.summary.by_hour_ist.length > 0);
  // The engine's own lessons name the two causes an operator needs on a flat day.
  assert(parsed.lessons.some((l) => l.includes("paper_naked_sell_disabled")));
  assert(parsed.lessons.some((l) => l.includes("without a completed LLM consensus")));
});

/* ---------- contract: what the engine writes is what the page can read ---------- */

const enginePath = new URL("./fixtures/decision-quality-engine.json", import.meta.url).pathname;
const engineFixture = () => JSON.parse(fs.readFileSync(enginePath, "utf8"));

test("every section the engine writes survives the parser", () => {
  // This fixture is produced by running the engine's own summarize(), not written by hand.
  // Hand-written fixtures are how the reader drifted: significance, by_playbook and the
  // probe counters were written by the engine for months and dropped silently here,
  // while the page went on printing a limitations note about t-statistics it never showed.
  const raw = engineFixture();
  const parsed = parseDecisionQuality(JSON.stringify(raw));
  assert(parsed);
  const carried = new Set(Object.keys(parsed));
  const dropped = Object.keys(raw).filter((key) => !carried.has(key));
  assert.deepEqual(dropped, [], `the parser drops sections the engine writes: ${dropped.join(", ")}`);
  // Nested counters drift the same way and are named here so the same test catches them.
  assert.deepEqual(Object.keys(raw.counts).filter((key) => !(key in parsed.counts)), []);
  assert.deepEqual(Object.keys(raw.recent[0]).filter((key) => !(key in parsed.recent[0])), []);
});

test("t-statistics are carried with their observation counts and correction note", () => {
  const parsed = parseDecisionQuality(JSON.stringify(engineFixture()));
  assert(parsed?.significance);
  const { significance } = parsed;
  assert.equal(significance.minimum_observations, 30);
  assert.equal(significance.multiple_testing_correction, "none");
  assert.equal(significance.forward_return_60m.observations, 36);
  assert.equal(typeof significance.forward_return_60m.t_statistic, "number");
  assert.equal(significance.trade_net_pnl.observations, 32);
  assert.equal(typeof significance.trade_net_pnl.t_statistic, "number");
  // The page's limitations list explains these numbers; it may not explain nothing.
  assert(parsed.limitations.some((note) => note.includes("t-statistics")));
});

test("a t-statistic below the minimum sample reads as unavailable, never as zero", () => {
  const raw = engineFixture();
  raw.significance.trade_net_pnl = { observations: 4, mean: null, standard_error: null, t_statistic: null };
  const parsed = parseDecisionQuality(JSON.stringify(raw));
  assert.equal(parsed?.significance?.trade_net_pnl.t_statistic, null);
  assert.equal(parsed?.significance?.trade_net_pnl.observations, 4);
});

test("playbook outcomes and probe counts are carried, and absent counters stay null", () => {
  const parsed = parseDecisionQuality(JSON.stringify(engineFixture()));
  assert(parsed);
  assert.equal(parsed.counts.probes, 5);
  assert(parsed.by_playbook.length >= 3);
  const trend = parsed.by_playbook.find((row) => row.playbook === "trend_following");
  assert(trend, "the engine's playbook rows must be readable by name");
  assert.equal(typeof trend.decisions, "number");
  assert.equal(typeof trend.probes, "number");
  assert(parsed.recent.some((row) => row.probe === true), "a probe decision must be distinguishable");
  assert(parsed.recent.every((row) => typeof row.playbook === "string"));

  // A report written before these counters existed still reads, and says so with null
  // rather than reporting zero probes it never counted.
  const older = engineFixture();
  delete older.significance;
  delete older.by_playbook;
  delete older.counts.probes;
  for (const row of older.recent) { delete row.probe; delete row.playbook; }
  const legacy = parseDecisionQuality(JSON.stringify(older));
  assert(legacy, "an older report must still parse");
  assert.equal(legacy.significance, null);
  assert.deepEqual(legacy.by_playbook, []);
  assert.equal(legacy.counts.probes, null);
  assert.equal(legacy.recent[0].probe, null);
  assert.equal(legacy.recent[0].playbook, null);
});

test("a malformed significance block never blanks the rest of the report", () => {
  for (const broken of ["none", 7, [], { forward_return_60m: {} }, { ...engineFixture().significance, minimum_observations: "30" }]) {
    const raw = engineFixture();
    raw.significance = broken;
    const parsed = parseDecisionQuality(JSON.stringify(raw));
    assert.ok(parsed, "a malformed significance block must not reject the whole report");
    assert.equal(parsed.significance, null);
  }
});

/* ---------- forecast scoring: what each decision claimed before the outcome existed ---------- */

test("both bases are carried, kept apart, and shown against both baselines", () => {
  const parsed = parseDecisionQuality(JSON.stringify(engineFixture()));
  assert(parsed?.forecast_scoring);
  const scoring = parsed.forecast_scoring;
  assert.equal(scoring.schema, FORECAST_SCORING_SCHEMA);
  assert.equal(scoring.minimum_scored, 30);
  assert.equal(scoring.decisions, parsed.counts.decisions);
  // A decision either states a forecast or it does not; the two counts partition the sample.
  assert.equal(scoring.with_forecast + scoring.unscoreable.no_forecast, scoring.decisions);
  // A refit ships a new basis and the numbers under the old one mean something else, so
  // the reader has to carry both rather than merge them into one curve.
  assert.deepEqual(scoring.by_basis.map((b) => b.basis), ["consensus_lean_v1", "consensus_lean_v2"]);
  const [v1, v2] = scoring.by_basis;
  assert.equal(v1.scored, 32);
  assert.equal(v1.insufficient_sample, false);
  assert.equal(v2.scored, 3);
  assert.equal(v2.insufficient_sample, true, "three scored forecasts cannot carry a skill claim");
  // Both baselines, because the coin is the flattering one and the gap between them is the point.
  assert.equal(typeof v1.baselines?.base_rate.brier_score, "number");
  assert.equal(typeof v1.baselines?.coin_flip.brier_score, "number");
  assert.equal(typeof v1.skill_vs_base_rate, "number");
  assert.equal(typeof v1.skill_vs_coin_flip, "number");
  assert(v1.verdict.includes("consensus_lean_v1"), "the engine's own sentence is what the panel prints");
  // Rows that could not be scored are counted and named, never folded into the misses.
  assert.equal(scoring.unscoreable.outcome_unresolved, 11);
  assert.equal(scoring.unscoreable.unknown_horizon, 1);
});

test("the four decomposition terms reconstruct the Brier score at the precision shown", () => {
  // The panel prints that identity as the caption under the table, so a reader can check
  // the arithmetic on the page. Four values rounded to six places do not re-add by
  // accident: the engine reconciles them against the published figures, and this is the
  // reader's copy of that promise.
  const parsed = parseDecisionQuality(JSON.stringify(engineFixture()));
  const bases = parsed?.forecast_scoring?.by_basis ?? [];
  assert(bases.length, "the fixture must carry a scored basis for this to mean anything");
  for (const basis of bases) {
    const d = basis.decomposition;
    assert(d, `a scored basis must carry its decomposition: ${basis.basis}`);
    const sum = Number((d.reliability - d.resolution + d.uncertainty + d.within_bin).toFixed(6));
    assert.equal(sum, basis.brier_score, `the published terms must add up as shown for ${basis.basis}`);
  }
});

test("reliability bins are carried with their counts, and an empty bin is not a zero", () => {
  const parsed = parseDecisionQuality(JSON.stringify(engineFixture()));
  const [v1, v2] = parsed?.forecast_scoring?.by_basis ?? [];
  assert(v1 && v2);
  assert.equal(v1.reliability.length, 10);
  assert.equal(v1.reliability.reduce((sum, bin) => sum + bin.forecasts, 0), v1.scored);
  // The three-forecast basis is the one that leaves bins empty, and the emptiness is what
  // is worth pinning: a bin nothing landed in must state nothing, not an observed
  // frequency of zero, which would draw as a real bar sitting at the floor. Asserted on a
  // basis that fills every bin, this check would pass without testing anything.
  const empty = v2.reliability.filter((bin) => bin.forecasts === 0);
  assert.equal(empty.length, 7, "the small basis must actually leave bins empty");
  assert(empty.every((bin) => bin.mean_forecast === null && bin.observed_frequency === null),
    "a bin nothing landed in states nothing, rather than an observed frequency of zero");
});

test("a scoring block declaring another schema is dropped, never relabelled", () => {
  // A refit of the scoring rules ships a new schema. Printing those numbers under these
  // labels is the same error as pooling two bases into one curve.
  const raw = engineFixture();
  raw.forecast_scoring.schema = "pramana.forecast_scoring.v2";
  const parsed = parseDecisionQuality(JSON.stringify(raw));
  assert.ok(parsed, "an unreadable scoring block must not reject the whole report");
  assert.equal(parsed.forecast_scoring, null);
});

test("a scoring block describing other rows than the report is refused", () => {
  const mutations: Array<(raw: ReturnType<typeof engineFixture>) => void> = [
    // Internally consistent, but computed over a different set of rows: the panel prints
    // "N of M decisions state a forecast" beside the report's own counts, so M must be
    // this report's M and not some other window's.
    (raw) => { raw.forecast_scoring.decisions += 1; raw.forecast_scoring.with_forecast += 1; },
    (raw) => { raw.forecast_scoring.with_forecast -= 1; },
    (raw) => { raw.forecast_scoring.by_basis[0].scored = raw.forecast_scoring.with_forecast + 1; },
    // Two rows for one mapping would be two curves claiming to describe it.
    (raw) => { raw.forecast_scoring.by_basis.push({ ...raw.forecast_scoring.by_basis[1], scored: 0 }); },
  ];
  for (const mutate of mutations) {
    const raw = engineFixture();
    mutate(raw);
    const parsed = parseDecisionQuality(JSON.stringify(raw));
    assert.ok(parsed, "the rest of the report stays readable");
    assert.equal(parsed.forecast_scoring, null,
      "a block that cannot describe this report's rows must not be shown beside its counts");
  }
});

test("a report written before forecast scoring still reads, and says null rather than empty", () => {
  const older = engineFixture();
  delete older.forecast_scoring;
  const parsed = parseDecisionQuality(JSON.stringify(older));
  assert(parsed, "an older report must still parse");
  // Null, not an empty block: "nothing was scored" and "this report never scored" are
  // different claims and the panel says different things about them.
  assert.equal(parsed.forecast_scoring, null);
  assert.equal(parsed.counts.decisions, 48);
});

test("a malformed scoring block never blanks the rest of the report", () => {
  for (const broken of ["none", 7, [], {}, { schema: FORECAST_SCORING_SCHEMA, by_basis: "no" }]) {
    const raw = engineFixture();
    raw.forecast_scoring = broken;
    const parsed = parseDecisionQuality(JSON.stringify(raw));
    assert.ok(parsed, "a malformed scoring block must not reject the whole report");
    assert.equal(parsed.forecast_scoring, null);
    assert(parsed.calibration.bins.length > 0, "the rest of the evidence is untouched");
  }
});

test("a skill score carries its sign, and an unavailable one is never rendered as zero", () => {
  assert.equal(signedRatio(0.677114), "+0.677");
  assert.equal(signedRatio(-9), "-9.000");
  assert.equal(signedRatio(0), "+0.000");
  assert.equal(signedRatio(null), DASH);
  assert.equal(signedRatio(Number.NaN), DASH);
});
