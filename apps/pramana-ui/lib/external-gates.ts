import fs from "node:fs";

export type ExternalGateCheck = {id: string; title: string; pass: boolean; detail: string};
const expected = [
  ["X01", "Real-feed session observation"],
  ["X02", "Sustained operational burn-in"],
  ["X03", "Strategy effectiveness"],
] as const;

/** Read the separately reviewed external gate report; absence is always pending. */
export function externalGateChecks(): ExternalGateCheck[] {
  const file = process.env.PRAMANA_EXTERNAL_GATE_REPORT?.trim();
  if (!file) return expected.map(([id, title]) => ({id, title, pass: false, detail: "External evidence has not been recorded."}));
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > 1_000_000) throw new Error("unsupported report bound");
    const report = JSON.parse(fs.readFileSync(file, "utf8")) as Record<string, unknown>;
    const gates = Array.isArray(report.gates) ? report.gates : [];
    const revision = typeof report.revision === "string" ? report.revision : "";
    const expectedRevision = process.env.PRAMANA_RELEASE_REVISION?.trim();
    const gateIds = gates.map((candidate) => candidate && typeof candidate === "object" ? (candidate as {id?: unknown}).id : undefined);
    const exactGateSet = gates.length === expected.length && new Set(gateIds).size === expected.length && expected.every(([id, title]) => {
      const candidate = gates.find((entry) => entry && typeof entry === "object" && (entry as {id?: unknown}).id === id) as {title?: unknown} | undefined;
      return gateIds.includes(id) && candidate?.title === title;
    });
    const reportReady = report.schema === "pramana.external_gate_report.v2" && report.ready === true && report.liveExecutionEnabled === false && exactGateSet && /^[0-9a-f]{40}$/i.test(revision) && typeof report.targetHost === "string" && report.targetHost.trim().length > 0 && (typeof expectedRevision === "string" && /^[0-9a-f]{40}$/i.test(expectedRevision) && expectedRevision === revision);
    return expected.map(([id, title]) => {
      const item = gates.find((candidate) => candidate && typeof candidate === "object" && (candidate as {id?: unknown}).id === id) as {id?: unknown; title?: unknown; passed?: unknown; detail?: unknown; evidenceSha256?: unknown} | undefined;
      const pass = reportReady && item?.title === title && item?.passed === true && typeof item.evidenceSha256 === "string" && /^[0-9a-f]{64}$/i.test(item.evidenceSha256);
      return {id, title, pass, detail: pass ? "Reviewed external evidence passed." : reportReady && typeof item?.detail === "string" && item.detail !== "passed" ? item.detail : "External evidence is missing or does not match this release."};
    });
  } catch {
    return expected.map(([id, title]) => ({id, title, pass: false, detail: "External gate report is unreadable, malformed or exceeds the supported bound."}));
  }
}
