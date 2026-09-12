import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import fs from "node:fs";

export const tenantId = process.env.PRAMANA_TENANT_ID || "default";

export function ledgerPath(): string {
  const configured = process.env.PRAMANA_LEDGER_PATH || "../../pramana_ledger.sqlite";
  return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
}

export function openLedger(): DatabaseSync | null {
  const target = ledgerPath();
  if (!fs.existsSync(/* turbopackIgnore: true */ target)) return null;
  const db = new DatabaseSync(target, { readOnly: true });
  db.exec("PRAGMA query_only = ON");
  return db;
}

export function hasTable(db: DatabaseSync, name: string): boolean {
  const row = db
    .prepare("SELECT 1 AS present FROM sqlite_master WHERE type='table' AND name=?")
    .get(name) as { present: number } | undefined;
  return Boolean(row?.present);
}

export type LedgerRow = {
  id: number;
  order_id: string;
  tenant_id: string;
  symbol: string;
  market: string;
  asset_class: string;
  side: "BUY" | "SELL";
  quantity: number;
  fill_price: string;
  notional: string;
  status: string;
  created_at: string;
};

export type CostRow = {
  id: number;
  order_id: string;
  tenant_id: string;
  code: string;
  amount: string;
  cash_debit: number;
  created_at: string;
};
