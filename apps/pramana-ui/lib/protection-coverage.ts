import type {Runtime} from "./pilot";

export function protectionCoverageCheck(runtime: Runtime, portfolio: {
  status: string; tenantId: string; markMode: string; ledgerId?: number; holdings: unknown[];
}, tenant: string, now = Date.now()) {
  const coverage = runtime.protectionCoverage;
  const age = now - Date.parse(coverage?.checkedAt || "");
  const current = runtime.status === "running" && runtime.mode === "paper"
    && portfolio.status === "ok" && portfolio.markMode === "engine_live"
    && portfolio.tenantId === tenant && coverage?.tenantId === tenant
    && coverage?.schema === "pramana.protection_coverage.v1"
    && Number.isSafeInteger(coverage.ledgerId) && coverage.ledgerId >= 0
    && coverage.ledgerId === portfolio.ledgerId && age >= -5000 && age <= 10000;
  const complete = coverage?.status === "complete" && coverage.issueCount === 0
    && Array.isArray(coverage.issues) && coverage.issues.length === 0
    && coverage.missingStopCount === 0 && coverage.invalidPositionCount === 0
    && coverage.positionCount === portfolio.holdings.length && coverage.coveredCount === coverage.positionCount;
  const issues = Array.isArray(coverage?.issues) ? coverage.issues.slice(0, 5)
    .map(issue => `${String(issue?.key).slice(0, 200)}: ${String(issue?.code).slice(0, 80)}`).join("; ") : "";
  return {
    id: "protection_coverage", title: "Stored position protection", pass: !!(current && complete),
    detail: !coverage ? "No stored-protection check recorded. New entries require valid stops."
      : `${current ? "Current" : "Unverified or outdated"}: ${coverage.coveredCount}/${coverage.positionCount} positions covered; ${coverage.issueCount} issues. ${issues ? issues + ". " : ""}Stored levels only; feed freshness and executable exit prices are separate checks.`,
  };
}
