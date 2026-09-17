import {createHash} from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {DatabaseSync} from "node:sqlite";
import {ledgerPath, tenantId} from "./db";
import type {PaperOmsObservation, PaperOmsRow} from "./paper-oms-types";

export const MAX_OMS_ORDERS = 5000;
export const MAX_OMS_DETAILS = 50;
const STATES = new Set(["CREATED", "RISK_APPROVED", "SUBMITTED", "SUBMISSION_UNCERTAIN",
  "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"]);
const TERMINAL = new Set(["FILLED", "CANCELLED", "REJECTED"]);
const HEX = /^[0-9a-f]{64}$/;
const hash = (raw: string) => createHash("sha256").update(raw).digest("hex");
function check(value: unknown): asserts value {
  if (!value) throw new Error("paper_oms_observation_invalid");
}
function text(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 128
    && value.trim() === value && !/[\x00-\x1f\x7f-\x9f\u2028\u2029]/u.test(value);
}
function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
function table(db: DatabaseSync, name: string): boolean {
  return !!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name);
}
function location(raw: string): string {
  check(raw.length > 0 && raw !== ":memory:");
  const selected = path.resolve(/* turbopackIgnore: true */ raw);
  const stat = fs.lstatSync(/* turbopackIgnore: true */ selected);
  check(stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1 && stat.size <= 268435456);
  return fs.realpathSync(/* turbopackIgnore: true */ selected);
}
function open(file: string): DatabaseSync {
  const db = new DatabaseSync(file, {readOnly: true});
  try {
    db.exec("PRAGMA query_only=ON; PRAGMA trusted_schema=OFF; PRAGMA busy_timeout=100; BEGIN");
    return db;
  } catch (error) { db.close(); throw error; }
}
/** No creation, migration, recovery calls or trade/entry-control writes. Separate read snapshots. */
export function readPaperOms(options: {ledger: string; oms?: string; tenant: string} = {
  ledger: ledgerPath(), oms: process.env.PRAMANA_OMS_DB, tenant: tenantId,
}): PaperOmsObservation {
  const result: PaperOmsObservation = {
    schema: "pramana.paper_oms_observation.v1", status: "unavailable",
    reason: "Selected paper order state is unavailable or inconsistent; operator review is required.",
    observedAt: new Date().toISOString(), tenantId: options.tenant,
    totalOrders: null, openOrders: null, recordedRecoveryAudits: null, orders: [], truncated: false,
    historyVerified: false, recoveryAuthorized: false, liveExecutionAuthorized: false,
  };
  let ledger: DatabaseSync | undefined, oms: DatabaseSync | undefined;
  try {
    check(text(options.tenant));
    const ledgerFile = location(options.ledger);
    ledger = open(ledgerFile);
    const binding = table(ledger, "paper_runtime_order_identity")
      ? ledger.prepare("SELECT payload,sha256 FROM paper_runtime_order_identity WHERE tenant_id=?")
        .get(options.tenant) : undefined;
    if (!binding && !options.oms) return {...result, status: "not_configured",
      reason: "No bound paper OMS is configured for this tenant. No empty-book or recovery claim is made."};
    check(binding && typeof binding.payload === "string" && binding.payload.length <= 262144
      && typeof binding.sha256 === "string" && HEX.test(binding.sha256)
      && hash(binding.payload) === binding.sha256);
    const configuration = JSON.parse(binding.payload);
    check(configuration && configuration.schema === "pramana.runtime_order_identity.v1"
      && configuration.mode === "bound_v1" && configuration.instruments
      && typeof configuration.instruments === "object" && !Array.isArray(configuration.instruments)
      && Object.keys(configuration.instruments).length > 0 && options.oms);
    const omsFile = location(options.oms);
    check(omsFile !== ledgerFile && hash(omsFile) === configuration.oms_path_sha256);
    oms = open(omsFile);
    check(table(oms, "oms_meta") && table(oms, "oms_orders") && table(oms, "oms_events")
      && table(oms, "oms_fills"));
    const version = oms.prepare("SELECT version FROM oms_meta WHERE id=1").get()?.version;
    check(version === 1 || version === 2);
    const rows = oms.prepare(`SELECT client_order_id,tenant_id,symbol,market,asset_class,side,state,
      requested_quantity,filled_quantity,updated_at,instrument_identity FROM oms_orders
      WHERE tenant_id=? ORDER BY updated_at DESC,client_order_id LIMIT ?`).all(options.tenant, MAX_OMS_ORDERS+1);
    check(rows.length <= MAX_OMS_ORDERS);
    const pending: PaperOmsRow[] = [];
    for (const row of rows) {
      check(row.tenant_id === options.tenant && text(row.client_order_id) && text(row.symbol)
        && row.market === "INDIA" && ["EQUITY", "ETF"].includes(String(row.asset_class))
        && (row.side === "BUY" || row.side === "SELL") && typeof row.state === "string"
        && STATES.has(row.state) && integer(row.requested_quantity) && row.requested_quantity > 0
        && integer(row.filled_quantity) && row.filled_quantity <= row.requested_quantity
        && (row.state !== "FILLED" || row.filled_quantity === row.requested_quantity)
        && typeof row.instrument_identity === "string"
        && Object.hasOwn(configuration.instruments, row.symbol)
        && row.instrument_identity === configuration.instruments[row.symbol]
        && typeof row.updated_at === "string" && row.updated_at.length <= 64
        && /(?:Z|[+-]\d{2}:\d{2})$/.test(row.updated_at)
        && Number.isFinite(Date.parse(row.updated_at)) && Date.parse(row.updated_at) <= Date.now()+5000);
      if (!TERMINAL.has(row.state)) pending.push({
        clientOrderId: row.client_order_id, symbol: row.symbol, side: row.side, state: row.state,
        requestedQuantity: row.requested_quantity, filledQuantity: row.filled_quantity,
        updatedAt: row.updated_at,
      });
    }
    check(table(ledger, "paper_ledger"));
    // An empty replacement at the pinned path is not evidence of an empty account.
    check(rows.length > 0 || !ledger.prepare("SELECT 1 FROM paper_ledger WHERE tenant_id=? LIMIT 1").get(options.tenant));
    const auditsPresent = table(oms, "oms_paper_recoveries");
    check(version !== 2 || auditsPresent);
    const recoveryAudits = auditsPresent
      ? oms.prepare("SELECT COUNT(*) AS n FROM oms_paper_recoveries WHERE tenant_id=?").get(options.tenant)?.n : 0;
    check(integer(recoveryAudits) && recoveryAudits <= rows.length && (version === 2 || recoveryAudits === 0));
    return {...result, status: "observed", totalOrders: rows.length, openOrders: pending.length,
      recordedRecoveryAudits: recoveryAudits, orders: pending.slice(0, MAX_OMS_DETAILS),
      truncated: pending.length > MAX_OMS_DETAILS,
      reason: "Stored local order states only. Full event/receipt reconciliation, reviewed recovery and entry release remain separate."};
  } catch {
    // Do not expose filesystem paths, SQL errors, another tenant or receipt contents.
    return result;
  } finally {
    try { oms?.close(); } finally { ledger?.close(); }
  }
}
