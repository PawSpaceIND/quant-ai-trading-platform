import { after, test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";

const dir = fs.mkdtempSync(path.join(os.tmpdir(), "research-workspace-test-"));
process.env.PRAMANA_TENANT_ID = "default";
process.env.PRAMANA_LEDGER_PATH = path.join(dir, "missing-ledger.sqlite");
process.env.PRAMANA_MARKET_SNAPSHOT = path.join(dir, "missing-market.json");
process.env.PRAMANA_CONSOLE_DB = path.join(dir, "console.sqlite");
process.env.PRAMANA_PROOF_DIR = path.join(dir, "missing-proofs");
const file = path.join(dir, "research.json");
after(() => fs.rmSync(dir, {recursive: true, force: true}));
const evidenceHash = "b".repeat(64);
function report() {
  const candidate = {
    decisions: 1, errors: 0, holds: 0, completed_episodes: 1, unfilled: 0, partial_entries: 0,
    unresolved_exits: 0, missing_decisions: 0, pending_buy_outcomes: 0,
    unknown_cost_decisions: 1, cost_total_complete: false, returned_models: ["synthetic-returned-model"],
    completed_case_pnl_inr: "-10", api_cost_usd: "0.001", total_latency_ms: "12",
    model_version: "synthetic-model", prompt_version: "synthetic-prompt",
    outcomes: [{case_id: "loss", status: "completed", filled_quantity: 10, net_pnl_inr: "-10"}],
    cost_stress: [1,2,3].map(multiplier => ({multiplier, status: "computed", completed_cases: 1, unresolved_exits: 0, unfilled: 0, completed_case_pnl_inr: String(-10 * multiplier)})),
  };
  return {
    schema: "pramana.research_workspace.v1", tenant_id: "default", generated_at: new Date().toISOString(),
    evidence_sha256: evidenceHash, experiment: "Synthetic comparison", mode: "historical", protocol_version: "fixture-v1",
    baseline: "baseline", expected_cases: 2, registered_cases: 1, resolved_cases: 1, unverified_adjustments: 1,
    costs: {capital_per_case: "1000", fee_bps: "10", slippage_bps: "10"},
    candidates: {candidate, baseline: structuredClone(candidate)},
    comparison_blockers: ["case_collection_incomplete", "corporate_action_adjustments_unverified"],
    limitations: ["Synthetic independent case tests; not portfolio returns."],
    status: "insufficient_evidence", automatic_promotion: false,
  };
}
function envelope(body: unknown) {
  const payload = JSON.stringify(body);
  return JSON.stringify({payload, sha256: createHash("sha256").update(payload).digest("hex")});
}

test("published report retains loss and coverage evidence without approving a strategy", async () => {
  const {parseResearchLabReport} = await import("../lib/research-lab");
  const parsed = parseResearchLabReport(envelope(report()), "default");
  assert.equal(parsed.candidates.candidate.completed_case_pnl_inr, "-10");
  assert.equal(parsed.expected_cases, 2);
  assert.equal(parsed.status, "insufficient_evidence");
  assert.equal(parsed.automatic_promotion, false);
  assert.equal(parsed.candidates.candidate.unknown_cost_decisions, 1);
  assert.equal(parsed.candidates.candidate.cost_total_complete, false);
  assert.deepEqual(parsed.candidates.candidate.returned_models, ["synthetic-returned-model"]);
});

test("tenant, digest, raw-input fields, false promotion and malformed metrics are rejected", async () => {
  const {parseResearchLabReport} = await import("../lib/research-lab");
  assert.throws(() => parseResearchLabReport(envelope(report()), "another-tenant"));
  const corrupt = JSON.parse(envelope(report())); corrupt.sha256 = "a".repeat(64);
  assert.throws(() => parseResearchLabReport(JSON.stringify(corrupt), "default"));
  const mutations = [
    (r: ReturnType<typeof report>) => Object.assign(r, {raw_packets: "PRIVATE_INPUT"}),
    (r: ReturnType<typeof report>) => Object.assign(r.candidates.candidate, {rationale: "PRIVATE_PROMPT"}),
    (r: ReturnType<typeof report>) => {r.automatic_promotion = true;},
    (r: ReturnType<typeof report>) => {r.candidates.candidate.cost_total_complete = true;},
    (r: ReturnType<typeof report>) => {r.candidates.candidate.completed_case_pnl_inr = "NaN";},
    (r: ReturnType<typeof report>) => {r.candidates.candidate.missing_decisions = 1;},
    (r: ReturnType<typeof report>) => {r.candidates.candidate.outcomes = [];},
    (r: ReturnType<typeof report>) => {r.generated_at = "2999-01-01T00:00:00Z";},
  ];
  for (const mutate of mutations) {const r = report(); mutate(r); assert.throws(() => parseResearchLabReport(envelope(r), "default"));}
});

test("missing or invalid configured snapshots fail closed without breaking workspace reads", async () => {
  const {readResearchLab} = await import("../lib/research-lab");
  delete process.env.PRAMANA_RESEARCH_LAB_REPORT;
  assert.equal(readResearchLab().status, "unavailable");
  process.env.PRAMANA_RESEARCH_LAB_REPORT = file;
  assert.equal(readResearchLab().status, "invalid");
  assert.equal(fs.existsSync(file), false);
  fs.writeFileSync(file, envelope(report()));
  assert.equal(readResearchLab().status, "published");
  fs.writeFileSync(file, "bad json");
  assert.equal(readResearchLab().report, null);
});

test("Atlas receives and persists identical bounded comparison context", async () => {
  fs.writeFileSync(file, envelope(report()));
  process.env.PRAMANA_RESEARCH_LAB_REPORT = file;
  process.env.ANTHROPIC_API_KEY = "synthetic-no-network-key";
  const {generateAnswer, conversations} = await import("../lib/copilot");
  let supplied = "";
  const transport: typeof fetch = async (_url, init) => {
    supplied = JSON.parse(String(init?.body)).system;
    return Response.json({content: [{type: "text", text: "Synthetic response"}], usage: {input_tokens: 10, output_tokens: 5}});
  };
  const response = await generateAnswer("Explain this experiment", undefined, "research-comparison-test", transport);
  assert.equal(response.status, "complete");
  const context = JSON.parse(conversations(response.id)[0].context!);
  assert.equal(context.researchLab.report.evidence_sha256, evidenceHash);
  assert.equal(context.researchLab.report.candidates.candidate.completed_case_pnl_inr, "-10");
  assert.equal(context.researchLab.report.case_details_included, false);
  assert.equal(context.researchLab.report.candidates.candidate.outcomes, undefined);
  assert(supplied.includes(JSON.stringify(context)));
  assert.equal(context.researchLab.report.automatic_promotion, false);
  assert.equal(context.researchLab.report.candidates.candidate.cost_total_complete, false);
  delete process.env.ANTHROPIC_API_KEY;
});
