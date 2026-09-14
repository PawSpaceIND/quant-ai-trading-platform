import fs from "node:fs";
import path from "node:path";

import { projectRoot } from "@/lib/db";

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
