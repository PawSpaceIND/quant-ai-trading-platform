/**
 * Reader for the engine's durable alert log.
 *
 * The engine raises seventeen alert codes and writes every one of them to a JSON-lines
 * file on the shared data volume. Some of those conditions reach the operator by another
 * route - a kill switch shows as a halt, a breached drawdown shows on its bar - but seven
 * have no other path to any screen at all, and one of them, MACRO_PROVIDER_UNAVAILABLE,
 * exists precisely because its failure is silent everywhere else: the engine's own note
 * records that a dead provider key cost two full sessions, with the pilot holding all day
 * and nothing in the logs a screen could show.
 *
 * Server-only. The file is written by another process, is append-only and unbounded, and
 * is shared, so this reads a bounded tail, keeps only this account's rows, and drops
 * anything it cannot parse rather than guessing.
 */
import fs from "node:fs";
import path from "node:path";

import { ledgerPath } from "@/lib/db";

/** Bytes read from the end of the log. A month of alerts is far smaller than this. */
export const ALERT_TAIL_BYTES = 256 * 1024;
/** Lines parsed from that tail, newest first. */
export const ALERT_SCAN_LIMIT = 400;
/** Rows returned to the page. */
export const ALERT_LIMIT = 50;
/** One line longer than this is malformed for our purposes and is skipped. */
const MAX_LINE_BYTES = 16 * 1024;

/** Codes the engine declares, with what each one means to an operator. */
export const ALERT_LABELS = {
  MAX_DRAWDOWN_BREACHED: "Drawdown limit breached",
  KILL_SWITCH_ENGAGED: "Trading halted",
  DAILY_LOSS_LIMIT_BREACHED: "Daily loss limit breached",
  PROVIDER_STALE: "Provider data stale",
  ZERODHA_SESSION_INVALID: "Broker session invalid",
  RISK_PROPOSAL_REJECTED: "Risk rejected a proposal",
  CADENCE_BRIEF: "Cadence brief",
  STOP_LOSS_TRIGGERED: "Stop loss triggered",
  TAKE_PROFIT_TRIGGERED: "Take profit triggered",
  SESSION_FLATTENED: "Session flattened",
  CADENCE_TICK_FAILED: "Cadence tick failed",
  LEARNING_EVIDENCE_DEGRADED: "Learning evidence degraded",
  POST_MORTEM_PENDING: "Post-mortem awaiting approval",
  SPECIALIST_WEIGHTS_UPDATED: "Specialist weights updated",
  SESSION_PLAN_READY: "Session plan ready",
  OVERNIGHT_GAP_UNEXPLAINED: "Unexplained overnight gap",
  MACRO_PROVIDER_UNAVAILABLE: "Macro provider unavailable",
} as const;
export type AlertCode = keyof typeof ALERT_LABELS;
export const ALERT_PRIORITIES = ["INFO", "HIGH", "CRITICAL"] as const;
export type AlertPriority = (typeof ALERT_PRIORITIES)[number];

export type Alert = {
  id: string;
  /** Null when the engine wrote a code this build does not declare. Never guessed. */
  code: AlertCode | null;
  rawCode: string;
  label: string;
  priority: AlertPriority | null;
  message: string;
  createdAt: string;
  /** When the line was written down, which can lag when it was raised. */
  loggedAt: string | null;
  metadata: Record<string, string>;
};
export type AlertsState =
  | { status: "available"; alerts: Alert[]; scanned: number; truncated: boolean; path: null }
  | { status: "unavailable" | "invalid"; detail: string; alerts: []; scanned: 0; truncated: false; path: null };

export function alertLogPath(): string {
  const configured = process.env.PRAMANA_ALERT_LOG;
  if (configured) return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
  // Mirrors quant_ai.config.paths.alert_log: beside the ledger on the shared volume.
  return path.join(/* turbopackIgnore: true */ path.dirname(ledgerPath()), "alerts.jsonl");
}

const text = (value: unknown, max: number): string | null =>
  typeof value === "string" && value.length <= max ? value : null;

function parseAlert(line: string): Alert | null {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  // Another account's alert is not this account's to display, and a row that does not
  // say whose it is cannot be attributed, so neither is shown.
  if (text(row.tenant_id, 80) !== (process.env.PRAMANA_TENANT_ID || "default")) return null;
  const id = text(row.notification_id, 120);
  const rawCode = text(row.code, 80);
  const createdAt = text(row.created_at, 80);
  if (!id || !rawCode || !createdAt || !Number.isFinite(Date.parse(createdAt))) return null;
  const priority = text(row.priority, 20);
  const loggedAt = text(row.logged_at, 80);
  const metadata: Record<string, string> = {};
  if (row.metadata && typeof row.metadata === "object" && !Array.isArray(row.metadata))
    for (const [key, entry] of Object.entries(row.metadata as Record<string, unknown>).slice(0, 12)) {
      const shown = text(entry, 300);
      if (shown !== null) metadata[key.slice(0, 60)] = shown;
    }
  const code = Object.hasOwn(ALERT_LABELS, rawCode) ? (rawCode as AlertCode) : null;
  return {
    id,
    code,
    rawCode,
    label: code ? ALERT_LABELS[code] : `Unrecognised code ${rawCode}`,
    priority: (ALERT_PRIORITIES as readonly string[]).includes(priority ?? "") ? (priority as AlertPriority) : null,
    message: text(row.message, 500) ?? "",
    createdAt,
    loggedAt: loggedAt && Number.isFinite(Date.parse(loggedAt)) ? loggedAt : null,
    metadata,
  };
}

/**
 * The newest alerts this account wrote, newest first.
 *
 * A missing file is `unavailable`, not an empty list: "the engine has raised nothing" and
 * "no log exists here" are different claims and only one of them is reassuring.
 */
export function readAlerts(limit = ALERT_LIMIT): AlertsState {
  const empty = { alerts: [] as [], scanned: 0 as const, truncated: false as const, path: null };
  const file = alertLogPath();
  let handle: number | undefined;
  try {
    const info = fs.statSync(/* turbopackIgnore: true */ file);
    if (!info.isFile()) return { status: "invalid", detail: "The alert log path is not a file.", ...empty };
    const length = Math.min(info.size, ALERT_TAIL_BYTES);
    const start = info.size - length;
    const buffer = Buffer.alloc(length);
    handle = fs.openSync(/* turbopackIgnore: true */ file, "r");
    const read = fs.readSync(handle, buffer, 0, length, start);
    let body = buffer.subarray(0, read).toString("utf8");
    // A tail almost always begins mid-line; a partial record is not a record.
    const truncated = start > 0;
    if (truncated) body = body.slice(body.indexOf("\n") + 1);
    const lines = body.split("\n").filter(line => line.length > 0 && Buffer.byteLength(line, "utf8") <= MAX_LINE_BYTES);
    const alerts: Alert[] = [];
    let scanned = 0;
    for (let index = lines.length - 1; index >= 0 && alerts.length < limit && scanned < ALERT_SCAN_LIMIT; index -= 1) {
      scanned += 1;
      const alert = parseAlert(lines[index]);
      if (alert) alerts.push(alert);
    }
    return { status: "available", alerts, scanned, truncated, path: null };
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code === "ENOENT")
      return { status: "unavailable", detail: "No alert log exists for this workspace. That is not the same as no alerts having been raised.", ...empty };
    return { status: "invalid", detail: "The alert log could not be read.", ...empty };
  } finally {
    if (handle !== undefined) try { fs.closeSync(handle); } catch { /* already gone */ }
  }
}
