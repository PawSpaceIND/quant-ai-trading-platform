import fs from "node:fs";
import {isResearchSnapshot, type ResearchSnapshot} from "@/components/research-lab-panel";

const MAX_BYTES = 512_000;

export type ResearchDashboardState = {
  status: "unavailable" | "invalid" | "published";
  detail: string;
  snapshot: ResearchSnapshot | null;
};

/** Read one operator-selected, sanitized export; never accept a request path or browser upload. */
export function readResearchDashboard(): ResearchDashboardState {
  const file = process.env.PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT;
  if (!file) {
    return {
      status: "unavailable",
      detail: "No sanitized research dashboard export has been published to this workspace.",
      snapshot: null,
    };
  }
  try {
    const stat = fs.statSync(/* turbopackIgnore: true */ file);
    if (!stat.isFile() || stat.size > MAX_BYTES) throw new Error("invalid_research_dashboard");
    const snapshot: unknown = JSON.parse(fs.readFileSync(/* turbopackIgnore: true */ file, "utf8"));
    if (!isResearchSnapshot(snapshot)) throw new Error("invalid_research_dashboard");
    return {
      status: "published",
      detail: "Sanitized paper-research export. Source timestamps and evidence status remain visible below.",
      snapshot,
    };
  } catch {
    return {
      status: "invalid",
      detail: "The configured research dashboard export is missing, invalid or exceeds the workspace limit.",
      snapshot: null,
    };
  }
}
