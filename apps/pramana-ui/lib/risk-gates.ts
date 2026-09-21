import type {RiskGate, Runtime} from "./pilot";

/**
 * The opt-in entry controls, presented so that "off" can never be mistaken for "quiet".
 *
 * Every gate here arms on data the operator supplies — a return history, a symbol
 * grouping, a session cap, an event file, a list of ex-dates — and an unarmed gate
 * produces exactly the same silence as an armed gate with nothing to say. That ambiguity
 * is the whole problem: an operator reading a page with no refusals on it has no way to
 * tell a book inside its limits from a book with no limits. So arming is the first thing
 * each row states, and the setting that arms it travels alongside, because the useful
 * response to "not armed" is knowing what to set.
 *
 * Nothing is estimated. Where a gate publishes an observed figure and the account could
 * not support one, the cause is shown in place of the number; a dash or a zero against a
 * cap would read as headroom the engine never claimed.
 */

export const RISK_GATES_SCHEMA = "pramana.risk_gates.v1";

/** `unverified` is a third state: the engine's own evidence is stale, foreign or absent. */
export type GateState = "armed" | "unarmed" | "unverified";

export type GateView = {
  id: string;
  title: string;
  state: GateState;
  setting: string;
  /** What the control refuses, in the operator's terms. */
  limits: string;
  /** Observed against the limit, or the recorded reason no figure can be given. */
  measure: string;
  /** What the operator supplied to arm it, or why it counts as unarmed. */
  source: string;
  /** Anything this gate is suppressing right now, e.g. a blackout in force. */
  active: string[];
};

const TITLES: Record<string, string> = {
  sector_concentration: "Group concentration",
  correlation_adjusted_gross: "Correlation-adjusted gross",
  book_expected_shortfall: "Book expected shortfall",
  overnight_exposure: "Overnight exposure",
  overnight_gap_monitor: "Overnight gap escalation",
  event_blackout: "Scheduled-event blackout",
  corporate_actions: "Declared corporate actions",
};

const LIMITS: Record<string, (gate: RiskGate) => string> = {
  sector_concentration: (g) => `No operator-declared group above ${percent(g.limit)} of equity. Refuses with sector_concentration_limit:<group>.`,
  correlation_adjusted_gross: (g) => `Gross counted at its own correlation, capped at ${percent(g.limit)} of equity. Refuses with correlation_adjusted_gross_limit.`,
  book_expected_shortfall: (g) => `95% one-day historical expected shortfall of the projected book, capped at ${percent(g.limit)} of equity. Refuses with book_expected_shortfall_limit.`,
  overnight_exposure: (g) => `Gross carried into a gap capped at ${percent(g.limit)} of equity, and no entry inside the final ${minutes(g.closingWindowSeconds)} of the session. Refuses with overnight_gross_limit, overnight_closing_window or overnight_entry_outside_session.`,
  overnight_gap_monitor: () => "Escalates a price step the engine cannot attribute to a declared action, and halts entries once one has gone a full session unresolved.",
  event_blackout: () => "Suppresses new entries on operator-scheduled events. Refuses with event_blackout:<category>. Exits are never blocked.",
  corporate_actions: () => "Declared ex-dates. A declaration is the only evidence that settles a re-based quote; without one the step guard still suspends the stop and nobody resolves it.",
};

/** A fraction rendered as a percentage, or an explicit absence. Never a bare dash. */
export function percent(value: number | null | undefined, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : "an unpublished limit";
}

function minutes(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${Math.round(value / 60)} minutes` : "an unpublished window";
}

function measureOf(gate: RiskGate, state: GateState): string {
  if (gate.observed === undefined && gate.observedUnavailable === undefined) return "";
  if (state !== "armed") return "";
  if (typeof gate.observed === "number" && Number.isFinite(gate.observed)) {
    return `Observed ${percent(gate.observed)} of equity against ${percent(gate.limit)}.`;
  }
  // The rule the rest of the workspace follows: name the cause, never show a zero that
  // would read as an empty book.
  return `Unavailable: ${String(gate.observedUnavailable || "unstated").slice(0, 200)}. No figure is shown because the account could not support one.`;
}

function sourceOf(gate: RiskGate, state: GateState): string {
  if (state === "unverified") return `Arming is unverified. Set through ${gate.setting}.`;
  const records = typeof gate.records === "number" && Number.isFinite(gate.records) ? gate.records : null;
  if (state === "unarmed") {
    return `Not armed. This control is off, which is not the same as having nothing to report. Arm it with ${gate.setting}.`;
  }
  if (gate.id === "sector_concentration") {
    const groups = typeof gate.groups === "number" && Number.isFinite(gate.groups) ? gate.groups : null;
    return `Armed from ${gate.setting} with ${records ?? "an unrecorded number of"} mapped symbol${records === 1 ? "" : "s"} across ${groups ?? "an unrecorded number of"} group${groups === 1 ? "" : "s"}. Unmapped symbols belong to no group and are left to the symbol cap.`;
  }
  if (records !== null) return `Armed from ${gate.setting} with ${records} declared record${records === 1 ? "" : "s"}.`;
  return `Armed through ${gate.setting}.`;
}

function activeOf(gate: RiskGate, state: GateState): string[] {
  if (state !== "armed" || !Array.isArray(gate.blackouts)) return [];
  return gate.blackouts.slice(0, 20).map((row) =>
    `${String(row?.symbol || "every instrument").slice(0, 40)} · ${String(row?.category || "unstated").slice(0, 60).replaceAll("_", " ")}`);
}

function valid(gates: Runtime["riskGates"]): gates is NonNullable<Runtime["riskGates"]> {
  return !!gates && gates.schema === RISK_GATES_SCHEMA && Array.isArray(gates.gates);
}

/**
 * Gate rows for the panel. An engine that is not running, or evidence belonging to
 * another tenant, yields `unverified` rows rather than a claim about arming — a stale
 * payload cannot vouch for what the running process has switched on.
 */
export function riskGateViews(runtime: Runtime, tenant: string, now = Date.now()): GateView[] {
  const gates = runtime.riskGates;
  if (!valid(gates)) return [];
  const age = now - Date.parse(gates.checkedAt || "");
  const current = runtime.status === "running" && runtime.mode === "paper"
    && gates.tenantId === tenant && age >= -5000 && age <= 10000;
  return gates.gates.slice(0, 20).filter((gate) => gate && typeof gate.id === "string").map((gate) => {
    const state: GateState = !current ? "unverified" : gate.armed === true ? "armed" : "unarmed";
    return {
      id: gate.id.slice(0, 60),
      title: TITLES[gate.id] || gate.id.replaceAll("_", " "),
      state,
      setting: String(gate.setting || "an unpublished setting").slice(0, 80),
      limits: (LIMITS[gate.id] || (() => "This gate published no threshold."))(gate),
      measure: measureOf(gate, state),
      source: sourceOf(gate, state),
      active: activeOf(gate, state),
    };
  });
}

/** Counts for the panel header. `unverified` is reported separately, never folded in. */
/** The tone the protective-controls badge may claim.
 *
 * Green means every gate is armed. Any non-zero count used to be enough, so one armed
 * gate out of seven showed green while six controls were switched off. */
export function gateTone(tally: {armed: number; unverified: number; total: number}) {
  if (tally.unverified) return "amber";
  if (!tally.total || !tally.armed) return "neutral";
  return tally.armed === tally.total ? "green" : "amber";
}

export function gateTally(views: GateView[]) {
  return {
    armed: views.filter((v) => v.state === "armed").length,
    unarmed: views.filter((v) => v.state === "unarmed").length,
    unverified: views.filter((v) => v.state === "unverified").length,
    total: views.length,
  };
}
