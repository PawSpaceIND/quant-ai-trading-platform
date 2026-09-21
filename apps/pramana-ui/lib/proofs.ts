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
  tenant_id?: string;
};

/**
 * Whether a proof file belongs to this account.
 *
 * The proof directory is shared and a trace written before this field existed does not
 * name its account, so a file alone cannot establish ownership. Both containers running
 * as the same tenant is a deployment fact, not a property of the file. "verified" means
 * the file names this account, or a tenant-scoped ledger row links the same order id.
 */
export type ProofOwnership = "verified" | "unverified";
export type ReadProof = { file: string; mtimeMs: number; proof: Proof; ownership: ProofOwnership };

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

/** Largest single trace this reader will parse. Every other evidence reader bounds
 * itself; the proof directory is unpruned and is re-read on every workspace poll. */
const PROOF_LIMIT_BYTES = 1_000_000;
/** Newest files considered when linking fills to traces. The directory only grows. */
const PROOF_INDEX_FILES = 2000;

export function readProofs(limit = 50): ReadProof[] {
  const directory = proofDirectory();
  const canonical = ledgerProofs().filter(({ proof }) => Array.isArray(proof.input_matrix));
  const orderIds = new Set(canonical.map(({ proof }) => proof.order_id));
  // A ledger-backed proof is already tenant-scoped in SQL, so it is owned by definition.
  const ledgerOrderIds = new Set(ledgerProofs().map(({ proof }) => proof.order_id).filter(Boolean));
  const files = !fs.existsSync(/* turbopackIgnore: true */ directory) ? [] : fs.readdirSync(/* turbopackIgnore: true */ directory)
    .filter((file) => file.endsWith(".json") && file !== "latest-backtest-tearsheet.json")
    .map((file) => {
      const full = path.join(/* turbopackIgnore: true */ directory, file);
      const stat = fs.statSync(/* turbopackIgnore: true */ full);
      return { file, full, mtimeMs: stat.mtimeMs, size: stat.size };
    })
    .filter(({ size }) => size <= PROOF_LIMIT_BYTES)
    .sort((a, b) => b.mtimeMs - a.mtimeMs)
    .slice(0, limit)
    .flatMap(({ file, full, mtimeMs }) => {
      try {
        const proof = JSON.parse(fs.readFileSync(/* turbopackIgnore: true */ full, "utf8")) as Proof;
        if (proof.order_id && orderIds.has(proof.order_id)) return [];
        // A file naming another account is not this workspace's evidence at all.
        if (typeof proof.tenant_id === "string" && proof.tenant_id !== tenantId) return [];
        return [{ file, mtimeMs, proof, ownership: fileOwnership(proof, ledgerOrderIds) }];
      } catch {
        return [];
      }
    });
  return [...canonical.map((item) => ({ ...item, ownership: "verified" as const })), ...files]
    .sort((a, b) => b.mtimeMs - a.mtimeMs).slice(0, limit);
}

/** Owned when the file names this account, or a tenant-scoped ledger row links its order. */
function fileOwnership(proof: Proof, ledgerOrderIds: Set<unknown>): ProofOwnership {
  if (proof.tenant_id === tenantId) return "verified";
  if (proof.order_id && ledgerOrderIds.has(proof.order_id)) return "verified";
  return "unverified";
}

export function latestSwarmIntelligence() {
  const proofs = readProofs();
  const usable = proofs.filter(({ proof }) => Array.isArray(proof.input_matrix));
  // This panel states what THIS account last decided, so an unowned trace cannot fill it.
  const latest = usable.find((item) => item.ownership === "verified");
  if (!latest) {
    return usable.length
      ? { status: "ownership_unverified", regime: "UNKNOWN", regimeSource: "not_persisted", consensus: "NO_PROOF", agents: [], proof: null }
      : { status: "empty", regime: "UNKNOWN", regimeSource: "not_persisted", consensus: "NO_PROOF", agents: [], proof: null };
  }
  const agents = (latest.proof.input_matrix ?? [])
    .filter((row): row is NonNullable<typeof row> => !!row && typeof row === "object" && !Array.isArray(row))
    .map((row) => ({
    agentId: row.agent_id ?? "unknown",
    domain: row.domain ?? "UNKNOWN",
    stance: row.stance ?? "NEUTRAL",
    confidence: Number(row.confidence ?? "0"),
    expectedReturn: Number(row.expected_return ?? "0"),
    expectedRisk: Number(row.expected_risk ?? "0"),
    participation: specialistParticipation(row),
  }));
  const directional = agents.filter(agent => !["RISK", "LIQUIDITY"].includes(agent.domain));
  const score = directional.reduce((sum, agent) => {
    const direction = agent.stance.includes("BUY") ? 1 : agent.stance.includes("SELL") || agent.stance === "AVOID" ? -1 : 0;
    return sum + direction * agent.confidence;
  }, 0);
  const consensus = directional.every(agent => agent.stance === "NEUTRAL") ? "NEUTRAL"
    : score > 0.35 ? "BULLISH" : score < -0.35 ? "BEARISH" : "MIXED";
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
  const ledgerLinked = new Set<string>();
  for (const item of ledgerProofs()) { index.set(item.proof.order_id!, item); ledgerLinked.add(String(item.proof.order_id)); }
  const directory = proofDirectory();
  if (!fs.existsSync(/* turbopackIgnore: true */ directory)) return index;
  const candidates = fs.readdirSync(/* turbopackIgnore: true */ directory)
    .filter((file) => file.endsWith(".json") && file !== "latest-backtest-tearsheet.json")
    .flatMap((file) => {
      try {
        const stat = fs.statSync(/* turbopackIgnore: true */ path.join(directory, file));
        return stat.size <= PROOF_LIMIT_BYTES ? [{ file, mtimeMs: stat.mtimeMs }] : [];
      } catch { return []; }
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs)
    .slice(0, PROOF_INDEX_FILES);
  for (const { file } of candidates) {
    let raw: string;
    try {
      raw = fs.readFileSync(/* turbopackIgnore: true */ path.join(directory, file), "utf8");
    } catch {
      continue;
    }
    if (!/"order_id"\s*:\s*"/.test(raw)) continue;
    try {
      const proof = JSON.parse(raw) as Proof;
      // Only an owned trace may be labelled an exact proof of this account's fill.
      if (typeof proof.order_id === "string" && proof.order_id && !index.has(proof.order_id)
          && (proof.tenant_id === tenantId || ledgerLinked.has(proof.order_id))) index.set(proof.order_id, { file, proof });
    } catch {
      // unreadable proof: leave the fill unlinked rather than guess
    }
  }
  return index;
}
