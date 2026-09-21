import fs from "node:fs";
import path from "node:path";

import { projectRoot, openLedger, hasTable, tenantId } from "@/lib/db";
import type { DecisionProvenance } from "@/lib/types";
import { providerFailures, specialistParticipation } from "@/lib/specialist-participation";

export type Proof = Record<string, unknown> & {
  decision_id?: string;
  generated_at?: string;
  subject?: string;
  input_matrix?: Array<Record<string, string>>;
  confidence_distribution?: Record<string, string>;
  proposal?: Record<string, string>;
  declared_rationales?: string[];
  stress_verdict?: Record<string, string>;
  risk_verdict?: Record<string, string>;
  market_regime?: string;
  regime?: string;
  order_id?: string;
};

export function proofDirectory(): string {
  const configured = process.env.PRAMANA_PROOF_DIR;
  if (configured) return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
  return path.join(/* turbopackIgnore: true */ projectRoot(), "pramana-proofs");
}

/** Keep full prompts/inputs in the private evidence store, outside dashboard responses. */
export function provenanceSummary(proof: Proof): DecisionProvenance {
  const record = (x: unknown): Record<string, unknown> => x && typeof x === "object" && !Array.isArray(x) ? x as Record<string, unknown> : {};
  const p = record(proof.provenance), i = record(p.inference);
  const text = (x: unknown) => typeof x === "string" && x ? x.slice(0, 200) : null;
  const hash = (x: unknown) => typeof x === "string" && /^[0-9a-f]{64}$/.test(x) ? x : null;
  if (p.schema !== "pramana.decision_provenance.v1") return {mode:"unrecorded",status:"unrecorded",provider:null,transport:null,requestedModel:null,resolvedModel:null,requestSha256:null,configurationSha256:null};
  return {mode:text(p.mode) ?? "unrecorded",status:text(i.status) ?? (p.mode === "deterministic" ? "not_called" : "unrecorded"),provider:text(i.provider),
    failureCode: typeof i.failure_code === "string" && Object.hasOwn(providerFailures, i.failure_code) ? i.failure_code : null,
    transport:text(i.transport),requestedModel:text(i.requested_model),resolvedModel:text(i.resolved_model),
    requestSha256:hash(i.request_sha256),configurationSha256:hash(p.configuration_sha256)};
}

export function readProofs(limit = 50): Array<{ file: string; mtimeMs: number; proof: Proof }> {
  const directory = proofDirectory();
  const canonical = ledgerProofs().filter(({ proof }) => Array.isArray(proof.input_matrix));
  const orderIds = new Set(canonical.map(({ proof }) => proof.order_id));
  const files = !fs.existsSync(/* turbopackIgnore: true */ directory) ? [] : fs.readdirSync(/* turbopackIgnore: true */ directory)
    .filter((file) => file.endsWith(".json") && file !== "latest-backtest-tearsheet.json")
    .map((file) => {
      const full = path.join(/* turbopackIgnore: true */ directory, file);
      return { file, full, mtimeMs: fs.statSync(/* turbopackIgnore: true */ full).mtimeMs };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs)
    .slice(0, limit)
    .flatMap(({ file, full, mtimeMs }) => {
      try {
        const proof = JSON.parse(fs.readFileSync(/* turbopackIgnore: true */ full, "utf8")) as Proof;
        if (proof.order_id && orderIds.has(proof.order_id)) return [];
        return [{ file, mtimeMs, proof }];
      } catch {
        return [];
      }
    });
  return [...canonical, ...files].sort((a, b) => b.mtimeMs - a.mtimeMs).slice(0, limit);
}

export function latestSwarmIntelligence() {
  const proofs = readProofs();
  const latest = proofs.find(({ proof }) => Array.isArray(proof.input_matrix));
  if (!latest) {
    return { status: "empty", regime: "UNKNOWN", regimeSource: "not_persisted", consensus: "NO_PROOF", agents: [], proof: null };
  }
  const agents = (latest.proof.input_matrix ?? []).map((row) => ({
    agentId: row.agent_id ?? "unknown",
    domain: row.domain ?? "UNKNOWN",
    stance: row.stance ?? "NEUTRAL",
    confidence: Number(row.confidence ?? "0"),
    expectedReturn: Number(row.expected_return ?? "0"),
    expectedRisk: Number(row.expected_risk ?? "0"),
    participation: specialistParticipation(row),
  }));
  const score = agents.reduce((sum, agent) => {
    if (["RISK", "LIQUIDITY"].includes(agent.domain)) return sum;
    const direction = agent.stance.includes("BUY") ? 1 : agent.stance.includes("SELL") || agent.stance === "AVOID" ? -1 : 0;
    return sum + direction * agent.confidence;
  }, 0);
  const consensus = score > 0.35 ? "BULLISH" : score < -0.35 ? "BEARISH" : "MIXED";
  const regime = String(latest.proof.market_regime ?? latest.proof.regime ?? "UNKNOWN");
  return {
    status: "ok",
    regime,
    regimeSource: regime === "UNKNOWN" ? "not_persisted_in_proof" : "proof",
    consensus,
    agents,
    proof: {
      file: latest.file,
      decisionId: latest.proof.decision_id ?? null,
      generatedAt: latest.proof.generated_at ?? null,
      subject: latest.proof.subject ?? null,
      rationale: latest.proof.declared_rationales ?? [],
      stress: latest.proof.stress_verdict ?? {},
      risk: latest.proof.risk_verdict ?? {},
      provenance: provenanceSummary(latest.proof),
    },
  };
}

/** Canonical evidence committed with a paper fill; older ledgers may lack these tables. */
function ledgerProofs(): Array<{ file: string; mtimeMs: number; proof: Proof }> {
  const result: Array<{ file: string; mtimeMs: number; proof: Proof }> = [];
  const db = openLedger();
  if (db) {
    try {
      for (const [table, schema, event] of [
        ["paper_protection_evidence", "pramana.protective_exit.v1", "protective_exit"],
        ["paper_decision_evidence", "pramana.swarm_fill.v1", "swarm_fill"],
      ]) {
        if (!hasTable(db, table)) continue;
        const rows = db.prepare(`SELECT order_id,payload FROM ${table} WHERE tenant_id=?`).all(tenantId) as {order_id:string;payload:string}[];
        for (const row of rows) {
          try {
            const proof = JSON.parse(row.payload) as Proof;
            if (proof.schema === schema && proof.event_type === event
                && proof.tenant_id === tenantId && proof.order_id === row.order_id
                && (event !== "swarm_fill" || Array.isArray(proof.input_matrix))) {
              const timestamp = Date.parse(String(proof.filled_at ?? proof.generated_at ?? ""));
              result.push({file: `ledger:${table}`, mtimeMs: Number.isFinite(timestamp) ? timestamp : 0, proof});
            }
          } catch { /* Corrupt evidence never receives an exact-match label. */ }
        }
      }
    } finally { db.close(); }
  }
  return result;
}

/** Exact fill links across the complete ledger and legacy file history, without a recency cap. */
export function proofsByOrderId(): Map<string, { file: string; proof: Proof }> {
  const index = new Map<string, { file: string; proof: Proof }>();
  for (const item of ledgerProofs()) index.set(item.proof.order_id!, item);
  const directory = proofDirectory();
  if (!fs.existsSync(/* turbopackIgnore: true */ directory)) return index;
  for (const file of fs.readdirSync(/* turbopackIgnore: true */ directory)) {
    if (!file.endsWith(".json") || file === "latest-backtest-tearsheet.json") continue;
    let raw: string;
    try {
      raw = fs.readFileSync(/* turbopackIgnore: true */ path.join(directory, file), "utf8");
    } catch {
      continue;
    }
    if (!/"order_id"\s*:\s*"/.test(raw)) continue;
    try {
      const proof = JSON.parse(raw) as Proof;
      if (typeof proof.order_id === "string" && proof.order_id && !index.has(proof.order_id)) index.set(proof.order_id, { file, proof });
    } catch {
      // unreadable proof: leave the fill unlinked rather than guess
    }
  }
  return index;
}
