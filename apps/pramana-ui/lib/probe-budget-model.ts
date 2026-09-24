import type {Runtime} from "./pilot";

/**
 * The exploration budget: how many holds a day Atlas may turn into small, labelled probe
 * entries, and how many it has used.
 *
 * The budget is operator-set (`PRAMANA_EXPLORATION_*`) and its cap defaults to 0. A cap
 * of 0 and a budget that found nothing worth probing are the same silence on a page, so
 * the engine publishes the budget it is running and this says which one it is, in words.
 * The day's count is read from the decision journal (`probe = 1`), the same record the
 * engine counts against, so a restart can neither reset it nor be hidden by it.
 *
 * Read-only. Nothing here raises the cap, lowers the bar, or makes a hard hold (a veto,
 * stale evidence, missing coverage) probeable.
 */

export const EXPLORATION_SCHEMA = "pramana.exploration.v1";
const IST_OFFSET_MS = 330 * 60_000;

export type Exploration = {
  schema: string; tenantId: string | null; checkedAt: string; setting: string;
  armed: boolean; maxPerDay: number;
  minWeightedScore: number | null; minConfidence: number | null; notionalFraction: number | null;
  withheldInRegimes: string[];
};

export type ProbesToday = {
  status: "available" | "unavailable";
  /** The IST session date the count covers. */
  sessionDate: string;
  count: number | null;
  detail: string;
};

/** The IST calendar date of `now`, and that day's bounds in the journal's own UTC format. */
export function istSession(now: Date): {date: string; since: string; until: string} {
  const local = new Date(now.getTime() + IST_OFFSET_MS);
  const date = local.toISOString().slice(0, 10);
  const start = Date.parse(`${date}T00:00:00Z`) - IST_OFFSET_MS;
  // `decided_at` is written as Python's isoformat of a UTC instant ("...+00:00"), so the
  // bounds are built in that shape; toISOString's "...Z" would not compare as a string.
  const stamp = (ms: number) => `${new Date(ms).toISOString().slice(0, 19)}+00:00`;
  return {date, since: stamp(start), until: stamp(start + 86_400_000)};
}

export type ProbeBudgetView = {
  state: "off" | "on" | "unverified";
  title: string;
  /** Each sentence the panel shows, in order. */
  lines: string[];
  /** "k of N today", or null when there is no budget to count against. */
  used: string | null;
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const fixed = (value: unknown, digits: number) => finite(value) ? value.toFixed(digits) : "an unpublished value";
const share = (value: unknown) => finite(value) ? `${(value * 100).toFixed(2)}%` : "an unpublished share";
const HARD_HOLD = "A hard hold (a specialist veto, stale evidence or missing coverage) is never probed; it stays a hold.";

function valid(block: unknown): block is Exploration {
  const b = block as Exploration | null | undefined;
  return !!b && b.schema === EXPLORATION_SCHEMA && typeof b.armed === "boolean"
    && Number.isSafeInteger(b.maxPerDay) && b.maxPerDay >= 0 && b.armed === b.maxPerDay > 0
    && Array.isArray(b.withheldInRegimes);
}

/**
 * The panel's words. A stale, foreign or malformed block is `unverified`: it cannot vouch
 * for what the running process applies, and it is never shown as off or on.
 */
export function probeBudgetView(runtime: Runtime, tenant: string, probes: ProbesToday | undefined, now = Date.now()): ProbeBudgetView {
  const block = runtime.exploration;
  const age = now - Date.parse(block?.checkedAt || "");
  const current = valid(block) && runtime.status === "running" && runtime.mode === "paper"
    && block.tenantId === tenant && age >= -5000 && age <= 10000;
  if (!current) {
    return {
      state: "unverified", title: "Probe budget not reported", used: null,
      lines: ["The running engine has not published its exploration budget, so this page cannot say whether probes are on or off.", HARD_HOLD],
    };
  }
  if (!block.armed) {
    return {
      state: "off", title: "Probes off: the cap is 0", used: null,
      lines: [
        `${block.setting} is 0, so no hold is turned into a probe. Every lean below the full-entry bar stays a hold.`,
        HARD_HOLD,
      ],
    };
  }
  const withheld = block.withheldInRegimes.slice(0, 10).map((item) => String(item).slice(0, 40).replaceAll("_", " "));
  const used = probes?.status === "available" && probes.count !== null
    ? `${probes.count} of ${block.maxPerDay} used today (${probes.sessionDate} IST)`
    : `today's count unavailable: ${probes?.detail ?? "not read"}`;
  return {
    state: "on", title: `Up to ${block.maxPerDay} probe${block.maxPerDay === 1 ? "" : "s"} a day`, used,
    lines: [
      `A hold whose specialists lean BUY at a weighted score of at least ${fixed(block.minWeightedScore, 2)}, with at least ${fixed(block.minConfidence, 2)} average confidence, may enter as a probe sized at ${share(block.notionalFraction)} of equity. Full entries are unchanged.`,
      withheld.length ? `Never in ${withheld.join(" or ")} regimes.` : "No regime withholds probes (regime routing is off).",
      "Every risk gate still applies to a probe, and each one is journaled with probe=1.",
      HARD_HOLD,
    ],
  };
}
