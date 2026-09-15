import {hasColumn, hasTable, openLedger, tenantId} from "./db";
import type {DatabaseSync} from "node:sqlite";

/**
 * Entry refusals, read from the decision journal the engine already writes.
 *
 * Every gate in the warden records its verdict on the proposal it refused:
 * `paper_decision_journal` carries one row per cadence decision with `governance` and the
 * exact `reason` string the gate returned — `sector_concentration_limit:<group>`,
 * `book_expected_shortfall_limit`, `overnight_gross_limit`, `event_blackout:<category>`,
 * `book_risk_measure_unavailable:<cause>` and the rest. Nothing new is stored here; this
 * reads what is already in the same ledger the valuations come from.
 *
 * The aggregate alone is not enough. A count of refusals over a window tells an operator
 * how often, never when or on what, and "a measurement stopped every entry for the last
 * hour" and "the book was genuinely full" both look like a number going up. So the
 * individual rows travel too, newest first.
 *
 * A missing journal is reported as unavailable and says so in those words. "No refusals
 * recorded" and "no record of refusals" are opposite claims, and only the engine's own
 * table can distinguish them.
 */

const TABLE = "paper_decision_journal";
/** Newest rows read for the per-decision list. Bounded so a long-lived ledger cannot page in. */
const RECENT_LIMIT = 25;
/** Newest rows scanned for the by-reason tally. */
const SCAN_LIMIT = 500;

export type RefusalReason = {reason: string; count: number; lastAt: string};
export type Refusal = {decidedAt: string; symbol: string; reason: string; stance: string};
export type GateRefusalState = {
  status: "available" | "unavailable";
  detail: string;
  /** Decisions and refusals the journal holds for this account, or null when unreadable. */
  decisions: number | null;
  rejected: number | null;
  /** Oldest decision in the scanned window, so the tally below has a stated span. */
  scannedSince: string | null;
  reasons: RefusalReason[];
  recent: Refusal[];
};

const unavailable = (detail: string): GateRefusalState => ({
  status: "unavailable", detail, decisions: null, rejected: null,
  scannedSince: null, reasons: [], recent: [],
});

const text = (value: unknown, max: number) => typeof value === "string" ? value.slice(0, max) : "";
const whole = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : null;

type JournalRow = {decided_at: string; symbol: string; reason: string | null; stance: string};

function read(db: DatabaseSync): GateRefusalState {
  if (!hasTable(db, TABLE)) {
    return unavailable(
      "The engine has recorded no decision journal for this account. Entry refusals cannot be shown, which is not evidence that none occurred.",
    );
  }
  for (const column of ["decided_at", "governance", "reason", "symbol", "stance"]) {
    if (!hasColumn(db, TABLE, column)) {
      return unavailable(
        `The decision journal in this ledger predates the ${column} column, so refusal reasons cannot be read from it.`,
      );
    }
  }
  // COUNT rather than SUM: SUM over an empty journal is NULL, and a null refusal count
  // would render as "unavailable" on a journal that is simply empty.
  const totals = db
    .prepare(
      `SELECT COUNT(*) AS decisions, COUNT(CASE WHEN governance='rejected' THEN 1 END) AS rejected FROM ${TABLE} WHERE tenant_id=?`,
    )
    .get(tenantId) as {decisions: number; rejected: number | null};
  const rows = db
    .prepare(
      `SELECT decided_at, symbol, reason, stance FROM ${TABLE} WHERE tenant_id=? AND governance='rejected' ORDER BY decided_at DESC LIMIT ${SCAN_LIMIT}`,
    )
    .all(tenantId) as JournalRow[];
  const tally = new Map<string, RefusalReason>();
  for (const row of rows) {
    // An unlabelled refusal is kept as such. Folding it into a neighbouring reason would
    // invent an attribution the journal never made.
    const reason = text(row.reason, 200) || "unstated";
    const decidedAt = text(row.decided_at, 60);
    const existing = tally.get(reason);
    if (existing) existing.count += 1;
    else tally.set(reason, {reason, count: 1, lastAt: decidedAt});
  }
  return {
    status: "available",
    detail: "Entry refusals as the engine journaled them, newest first. Each reason is the exact verdict string the gate returned. Exits are never refused by these controls; a refusal here stopped a new entry only.",
    decisions: whole(totals?.decisions),
    rejected: whole(totals?.rejected),
    scannedSince: rows.length ? text(rows[rows.length - 1].decided_at, 60) : null,
    reasons: [...tally.values()].sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason)).slice(0, 25),
    recent: rows.slice(0, RECENT_LIMIT).map((row) => ({
      decidedAt: text(row.decided_at, 60),
      symbol: text(row.symbol, 40),
      reason: text(row.reason, 200) || "unstated",
      stance: text(row.stance, 20),
    })),
  };
}

/** Never throws: a workspace response must not be lost to an unreadable evidence table. */
export function readGateRefusals(connection?: DatabaseSync): GateRefusalState {
  const db = connection ?? openLedger();
  if (!db) return unavailable("No paper ledger is readable, so journaled entry refusals cannot be shown.");
  try {
    return read(db);
  } catch {
    return unavailable("The decision journal could not be read. Refusal history is unavailable; this is not a statement that there were none.");
  } finally {
    if (!connection) db.close();
  }
}
