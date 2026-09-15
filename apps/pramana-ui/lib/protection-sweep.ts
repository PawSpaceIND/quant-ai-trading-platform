import type {ProtectionSweep, Runtime} from "./pilot";

/**
 * Whether the engine can currently act on the stops it has stored.
 *
 * `protection-coverage.ts` proves a level exists in the ledger. This is the other half,
 * and it is the half that goes wrong silently: a symbol the sweep could not price, or one
 * whose quote a corporate action re-based, is carrying a stop that will not fire. The
 * engine already halts on both, but only after a grace period or a full session, and
 * until then nothing on the page changed. An operator in that window is running an
 * unprotected position and has no way to know it.
 *
 * Currency is judged exactly as the stored-protection check judges it, against the same
 * ten-second window, because a sweep result older than that describes a book the engine
 * has already re-marked several times.
 */

/** The window a sweep result stays current for, matching the stored-protection check. */
const MAX_AGE_MS = 10000;
const FUTURE_TOLERANCE_MS = 5000;
export const SWEEP_SCHEMA = "pramana.protection_sweep.v1";

export type ProtectionSeverity = "clear" | "unverified" | "exposed";

export type ProtectionAlertState = {
  severity: ProtectionSeverity;
  /** Symbols whose stop is not being enforced right now, unpriced and suspended together. */
  symbols: string[];
  headline: string;
  detail: string;
};

const list = (values: string[], limit = 8) =>
  values.length > limit ? `${values.slice(0, limit).join(", ")} and ${values.length - limit} more` : values.join(", ");

const seconds = (value: number | null | undefined) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${Math.round(value)}s` : "an unrecorded interval";

function usable(sweep: ProtectionSweep | null | undefined): sweep is ProtectionSweep {
  return !!sweep && sweep.schema === SWEEP_SCHEMA
    && Array.isArray(sweep.unprotected) && Array.isArray(sweep.rebased)
    && !!sweep.gapMonitor && typeof sweep.gapMonitor.armed === "boolean"
    && Array.isArray(sweep.gapMonitor.unresolved);
}

const unpriced = (sweep: ProtectionSweep) =>
  sweep.unprotected.map((row) => String(row?.symbol ?? "").slice(0, 40)).filter(Boolean);
const suspended = (sweep: ProtectionSweep) =>
  sweep.rebased.map((symbol) => String(symbol ?? "").slice(0, 40)).filter(Boolean);
const unresolved = (sweep: ProtectionSweep) =>
  sweep.gapMonitor.unresolved.map((row) => String(row?.symbol ?? "").slice(0, 40)).filter(Boolean);

/**
 * The banner state. Deliberately three-valued: a sweep the page cannot verify is not the
 * same claim as a sweep that found nothing, and neither may be rendered as "clear".
 */
export function protectionAlert(runtime: Runtime, tenant: string, now = Date.now()): ProtectionAlertState {
  const sweep = runtime.protectionSweep;
  const age = now - Date.parse(sweep?.checkedAt || "");
  const running = runtime.status === "running" && runtime.mode === "paper";
  const current = running && usable(sweep) && sweep.tenantId === tenant
    && age >= -FUTURE_TOLERANCE_MS && age <= MAX_AGE_MS;
  if (!usable(sweep)) {
    return {
      severity: "unverified", symbols: [],
      headline: "Stop enforcement is unverified",
      detail: "The engine published no protection-sweep state. Whether the stored stops can be acted on is unknown; this is not a statement that they can.",
    };
  }
  const exposed = [...new Set([...unpriced(sweep), ...suspended(sweep)])].sort();
  const open = unresolved(sweep);
  if (!current) {
    return {
      severity: "unverified", symbols: exposed,
      headline: "Stop enforcement is unverified",
      detail: `Sweep evidence is outdated, foreign or from an engine that is not running. Last recorded sweep ${sweep.sweptAt || "never"}. Refresh before relying on it.`,
    };
  }
  if (sweep.sweptAt === null) {
    // The one case an empty list must never be read as good news.
    return {
      severity: "unverified", symbols: [],
      headline: "No protection sweep has run",
      detail: "The engine has not completed a protective sweep since it started, so no stored stop has been evaluated yet. An empty exposure list here means nothing has been checked, not that nothing is wrong.",
    };
  }
  if (exposed.length) {
    const parts: string[] = [];
    if (unpriced(sweep).length) {
      const since = sweep.unprotected.find((row) => row?.unpricedSince)?.unpricedSince;
      parts.push(`${list(unpriced(sweep))} could not be priced${since ? ` (unpriced since ${since})` : ""}; entries halt after ${seconds(sweep.haltAfterSeconds)} of this`);
    }
    if (suspended(sweep).length) {
      parts.push(`${list(suspended(sweep))} had its quote re-based by a corporate action, so the stored stop and cost basis are not comparable and the stop is held until they agree again`);
    }
    return {
      severity: "exposed", symbols: exposed,
      headline: `${exposed.length} position${exposed.length === 1 ? "" : "s"} carrying an unenforced stop`,
      detail: `${parts.join(". ")}. The affected positions are still held and protective exits keep sweeping; these stops are simply not being evaluated. Reconcile before the halt decides for you.`,
    };
  }
  if (open.length) {
    return {
      severity: "exposed", symbols: open,
      headline: `${open.length} unexplained price gap${open.length === 1 ? "" : "s"} awaiting an operator`,
      detail: `${list(open)}: a step larger than the exchange band that the engine could not attribute to a declared corporate action. Entries stop once one has gone a full session unresolved.`,
    };
  }
  return {
    severity: "clear", symbols: [],
    headline: "Every stored stop was evaluated on the last sweep",
    detail: `Swept ${sweep.sweptAt}. No position was left unpriced or suspended${sweep.gapMonitor.armed ? " and no price gap is unresolved" : ""}.`,
  };
}

/**
 * The pilot-readiness entry. Same grain as `protectionCoverageCheck`: a pass requires
 * current, tenant-bound evidence from a running engine, and every failure says which of
 * those it was rather than collapsing into one word.
 */
export function protectionSweepCheck(runtime: Runtime, tenant: string, now = Date.now()) {
  const sweep = runtime.protectionSweep;
  const alert = protectionAlert(runtime, tenant, now);
  const monitor = usable(sweep) && sweep.gapMonitor.armed
    ? "Overnight gap monitor armed."
    : "Overnight gap monitor not armed: an undeclared re-basing still suspends the stop, but nothing escalates it.";
  return {
    id: "protection_sweep",
    title: "Live stop enforcement",
    pass: alert.severity === "clear",
    detail: `${alert.detail} ${monitor} Enforcement only; whether a stop is stored at all is the separate stored-protection check.`,
  };
}
