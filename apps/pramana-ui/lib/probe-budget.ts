import type {DatabaseSync} from "node:sqlite";
import {hasColumn, hasTable, openLedger, tenantId} from "./db";
import {istSession, type ProbesToday} from "./probe-budget-model";

/** Server-side reader for the probe budget panel; the pure model lives beside it so the
 * client panel never pulls the ledger driver into its bundle. */
export * from "./probe-budget-model";

const TABLE = "paper_decision_journal";

/** Probes the journal holds for today's IST session. Never throws. */
export function readProbesToday(connection?: DatabaseSync, now = new Date()): ProbesToday {
  const {date, since, until} = istSession(now);
  const unavailable = (detail: string): ProbesToday => ({status: "unavailable", sessionDate: date, count: null, detail});
  const db = connection ?? openLedger();
  if (!db) return unavailable("No paper ledger is readable, so today's probes cannot be counted.");
  try {
    if (!hasTable(db, TABLE)) return unavailable("The engine has recorded no decision journal for this account yet.");
    if (!hasColumn(db, TABLE, "probe")) return unavailable("The decision journal in this ledger predates probe labelling.");
    const row = db
      .prepare(`SELECT COUNT(*) AS probes FROM ${TABLE} WHERE tenant_id=? AND probe=1 AND decided_at>=? AND decided_at<?`)
      .get(tenantId, since, until) as {probes: number} | undefined;
    const count = Number.isSafeInteger(row?.probes) && (row!.probes as number) >= 0 ? row!.probes : null;
    return count === null
      ? unavailable("The journal returned no readable probe count.")
      : {status: "available", sessionDate: date, count, detail: "Journaled probes for today's IST session."};
  } catch {
    return unavailable("The decision journal could not be read; today's probe count is unknown.");
  } finally {
    if (!connection) db.close();
  }
}

