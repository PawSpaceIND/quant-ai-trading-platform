import fs from "node:fs";
import path from "node:path";
import { ledgerPath } from "./db";
export type ResearchReport = {
  schema: string;
  strategy: string;
  scope: string;
  created_at: string;
  data_sha256?: string;
  report_sha256?: string;
  candidate_trials: number;
  holdout: { net_return: number; max_drawdown: number; observations: number };
  buy_and_hold: { net_return: number };
  path_stress: { max_drawdown_p95: number };
  limitations: string[];
};
export function readResearch(): ResearchReport | null {
  try {
    const file =
      process.env.PRAMANA_RESEARCH_REPORT ||
      path.join(path.dirname(ledgerPath()), "research-report.json");
    if (fs.statSync(/* turbopackIgnore: true */ file).size > 2_000_000)
      return null;
    const report = JSON.parse(
      fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"),
    );
    if (
      report.schema !== "pramana.research.v1" ||
      !Number.isFinite(report.holdout?.net_return) ||
      !Array.isArray(report.limitations)
    )
      return null;
    return report;
  } catch {
    return null;
  }
}
