import fs from "node:fs";
import path from "node:path";

import { projectRoot, openLedger, hasTable, tenantId } from "@/lib/db";

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

export function readProofs(limit = 50): Array<{ file: string; mtimeMs: number; proof: Proof }> {
  const directory = proofDirectory();
  if (!fs.existsSync(/* turbopackIgnore: true */ directory)) return [];
  return fs.readdirSync(/* turbopackIgnore: true */ directory)
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
        return [{ file, mtimeMs, proof }];
      } catch {
        return [];
      }
    });
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
  }));
  const score = agents.reduce((sum, agent) => {
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
    },
  };
}

/**
 * Every persisted proof that produced a fill, keyed by broker order id.
 *
 * Scans the whole directory rather than the newest N files: a fill's proof is
 * exactly as old as the fill, so any recency cap would silently drop the link
 * for everything but the latest decisions. Files are pre-filtered on a cheap
 * substring test before being parsed.
 */
export function proofsByOrderId(): Map<string, { file: string; proof: Proof }> {
  const index = new Map<string, { file: string; proof: Proof }>();
  const db = openLedger();
  if (db) {
    try {
      if (hasTable(db, "paper_protection_evidence")) {
        const rows = db.prepare("SELECT order_id,payload FROM paper_protection_evidence WHERE tenant_id=?").all(tenantId) as {order_id:string;payload:string}[];
        for (const row of rows) {
          try {
            const proof = JSON.parse(row.payload) as Proof;
            if (proof.schema === "pramana.protective_exit.v1" && proof.event_type === "protective_exit"
                && proof.tenant_id === tenantId && proof.order_id === row.order_id) {
              index.set(row.order_id, {file: "ledger:paper_protection_evidence", proof});
            }
          } catch { /* Corrupt evidence never receives an exact-match label. */ }
        }
      }
    } finally { db.close(); }
  }
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
