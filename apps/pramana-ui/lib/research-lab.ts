import fs from "node:fs";
import { createHash } from "node:crypto";
import { tenantId } from "./db";

export type LabOutcome = {case_id: string; status: "completed" | "unfilled" | "unresolved_exit"; filled_quantity: number; net_pnl_inr: string | null};
export type LabStress = {multiplier: number; status: "computed" | "unavailable"; completed_cases: number | null; unresolved_exits: number | null; unfilled: number | null; completed_case_pnl_inr: string | null};
export type LabCandidate = {
  decisions: number; errors: number; holds: number; completed_episodes: number;
  unfilled: number; partial_entries: number; unresolved_exits: number;
  missing_decisions: number; pending_buy_outcomes: number;
  unknown_cost_decisions: number; cost_total_complete: boolean; returned_models: string[];
  completed_case_pnl_inr: string; api_cost_usd: string; total_latency_ms: string;
  model_version: string | null; prompt_version: string | null;
  outcomes: LabOutcome[]; cost_stress: LabStress[];
};
export type LabReport = {
  schema: "pramana.research_workspace.v1"; tenant_id: string; generated_at: string;
  evidence_sha256: string; experiment: string; mode: "historical" | "forward_paper";
  protocol_version: string; baseline: string; expected_cases: number; registered_cases: number;
  resolved_cases: number; unverified_adjustments: number;
  costs: {capital_per_case: string; fee_bps: string; slippage_bps: string};
  candidates: Record<string, LabCandidate>; comparison_blockers: string[]; limitations: string[];
  status: "insufficient_evidence"; automatic_promotion: false;
};
export type ResearchLabState = {status: "unavailable" | "invalid" | "published"; detail: string; report: LabReport | null};
const counts = ["decisions", "errors", "holds", "completed_episodes", "unfilled", "partial_entries", "unresolved_exits", "missing_decisions", "pending_buy_outcomes", "unknown_cost_decisions"] as const;
function check(condition: unknown): asserts condition {if (!condition) throw new Error("invalid_research_snapshot");}
function exact(value: unknown, keys: readonly string[]): asserts value is Record<string, unknown> {
  check(value && typeof value === "object" && !Array.isArray(value));
  check(Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k)));
}
function label(value: unknown): boolean {return typeof value === "string" && value.trim().length > 0 && value.length <= 1000;}
function count(value: unknown): boolean {return Number.isSafeInteger(value) && Number(value) >= 0;}
function decimal(value: unknown, signed = false): boolean {
  return typeof value === "string" && value.length < 100 && value.trim() !== "" && Number.isFinite(Number(value)) && (signed || Number(value) >= 0);
}
function list(value: unknown, limit: number): asserts value is unknown[] {check(Array.isArray(value) && value.length <= limit);}

/** Exact envelope hashing detects accidental changes, not a malicious publisher. */
export function parseResearchLabReport(raw: string, tenant: string): LabReport {
  check(Buffer.byteLength(raw) <= 2_000_000);
  const envelope = JSON.parse(raw);
  exact(envelope, ["payload", "sha256"]);
  check(typeof envelope.payload === "string" && typeof envelope.sha256 === "string");
  check(createHash("sha256").update(envelope.payload).digest("hex") === envelope.sha256);
  const r = JSON.parse(envelope.payload);
  exact(r, ["schema", "tenant_id", "generated_at", "evidence_sha256", "experiment", "mode", "protocol_version", "baseline", "expected_cases", "registered_cases", "resolved_cases", "unverified_adjustments", "costs", "candidates", "comparison_blockers", "limitations", "status", "automatic_promotion"]);
  check(r.schema === "pramana.research_workspace.v1" && r.tenant_id === tenant);
  check(r.status === "insufficient_evidence" && r.automatic_promotion === false);
  check(label(r.generated_at) && /(Z|[+-]\d\d:\d\d)$/.test(String(r.generated_at)) && Number.isFinite(Date.parse(String(r.generated_at))) && Date.parse(String(r.generated_at)) <= Date.now() + 300000);
  check(typeof r.evidence_sha256 === "string" && /^[0-9a-f]{64}$/.test(r.evidence_sha256));
  for (const k of ["experiment", "protocol_version", "baseline"]) check(label(r[k]));
  check(r.mode === "historical" || r.mode === "forward_paper");
  for (const k of ["expected_cases", "registered_cases", "resolved_cases", "unverified_adjustments"]) check(count(r[k]));
  check(Number(r.expected_cases) > 0 && Number(r.registered_cases) <= 5000 && Number(r.registered_cases) <= Number(r.expected_cases) && Number(r.resolved_cases) <= Number(r.registered_cases) && Number(r.unverified_adjustments) <= Number(r.registered_cases));
  exact(r.costs, ["capital_per_case", "fee_bps", "slippage_bps"]);
  for (const n of Object.values(r.costs)) check(decimal(n));
  check(Number(r.costs.capital_per_case) > 0 && Number(r.costs.fee_bps) < 10000 && Number(r.costs.slippage_bps) < 10000);
  for (const k of ["comparison_blockers", "limitations"]) {list(r[k], 200); check(r[k].every(label));}
  check(r.candidates && typeof r.candidates === "object" && !Array.isArray(r.candidates));
  const candidates = Object.entries(r.candidates);
  check(candidates.length >= 2 && candidates.length <= 16 && Object.hasOwn(r.candidates, String(r.baseline)));
  for (const [name, c] of candidates) {
    check(label(name));
    exact(c, [...counts, "completed_case_pnl_inr", "api_cost_usd", "cost_total_complete", "total_latency_ms", "model_version", "returned_models", "prompt_version", "outcomes", "cost_stress"]);
    for (const k of counts) check(count(c[k]) && Number(c[k]) <= Number(r.registered_cases));
    check(Number(c.decisions) + Number(c.missing_decisions) === r.registered_cases);
    check(Number(c.errors) + Number(c.holds) + Number(c.completed_episodes) + Number(c.unfilled) + Number(c.unresolved_exits) + Number(c.pending_buy_outcomes) === c.decisions);
    check(decimal(c.completed_case_pnl_inr, true) && decimal(c.api_cost_usd) && decimal(c.total_latency_ms));
    check(c.cost_total_complete === (c.unknown_cost_decisions === 0) && Number(c.unknown_cost_decisions) <= Number(c.decisions));
    list(c.returned_models, 16); check(c.returned_models.every(label));
    check((c.model_version === null || label(c.model_version)) && (c.prompt_version === null || label(c.prompt_version)));
    list(c.outcomes, 5000);
    const caseIds = new Set<string>();
    for (const o of c.outcomes) {
      exact(o, ["case_id", "status", "filled_quantity", "net_pnl_inr"]);
      check(label(o.case_id) && !caseIds.has(String(o.case_id)) && count(o.filled_quantity)); caseIds.add(String(o.case_id));
      check(["completed", "unfilled", "unresolved_exit"].includes(String(o.status)));
      check(o.status === "completed" ? decimal(o.net_pnl_inr, true) : o.net_pnl_inr === null);
    }
    for (const [status, key] of [["completed", "completed_episodes"], ["unfilled", "unfilled"], ["unresolved_exit", "unresolved_exits"]]) check(c.outcomes.filter(o => (o as LabOutcome).status === status).length === c[key]);
    list(c.cost_stress, 3); check(c.cost_stress.length === 3);
    for (const [i, s] of c.cost_stress.entries()) {
      exact(s, ["multiplier", "status", "completed_cases", "unresolved_exits", "unfilled", "completed_case_pnl_inr"]);
      check(s.multiplier === i + 1);
      check(s.status === "computed" || s.status === "unavailable");
      if (s.status === "computed") {
        check(count(s.completed_cases) && count(s.unresolved_exits) && count(s.unfilled) && decimal(s.completed_case_pnl_inr, true));
        check(Number(s.completed_cases) + Number(s.unresolved_exits) + Number(s.unfilled) === c.outcomes.length);
      } else check([s.completed_cases, s.unresolved_exits, s.unfilled, s.completed_case_pnl_inr].every(v => v === null));
    }
  }
  return r as unknown as LabReport;
}

export function readResearchLab(): ResearchLabState {
  const file = process.env.PRAMANA_RESEARCH_LAB_REPORT;
  if (!file) return {status: "unavailable", detail: "No experiment comparison has been published to this workspace.", report: null};
  try {
    const stat = fs.statSync(/* turbopackIgnore: true */ file);
    check(stat.isFile() && stat.size <= 2_000_000);
    const report = parseResearchLabReport(fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"), tenantId);
    return {status: "published", detail: "Published offline snapshot. Completeness and evidence quality still require review.", report};
  } catch {
    return {status: "invalid", detail: "The configured comparison is missing, invalid or belongs to another tenant. Republish a valid report; it does not qualify any readiness gate.", report: null};
  }
}

export function researchLabContext() {
  const state = readResearchLab();
  if (!state.report) return state;
  // Persist exactly the bounded context sent to Atlas; case lists stay in the UI/export.
  const {candidates, ...report} = state.report;
  return {...state, report: {...report, case_details_included: false,
    candidates: Object.fromEntries(Object.entries(candidates).map(([name, {outcomes, ...candidate}]) =>
      [name, {...candidate, case_outcome_count: outcomes.length}]))}};
}
