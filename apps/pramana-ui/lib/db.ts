import { DatabaseSync } from "node:sqlite";
import path from "node:path";
import fs from "node:fs";

export const tenantId = process.env.PRAMANA_TENANT_ID || "default";

const ROOT_MARKERS = ["pyproject.toml", ".git"];

/**
 * Walk upwards from the process cwd to the repository root, mirroring
 * `src/quant_ai/config/paths.py::project_root` so that the UI and the Python
 * CLI/daemon agree on the default ledger location with no configuration.
 */
export function projectRoot(): string {
  let current = path.resolve(/* turbopackIgnore: true */ process.cwd());
  for (;;) {
    if (ROOT_MARKERS.some((marker) => fs.existsSync(/* turbopackIgnore: true */ path.join(current, marker)))) {
      return current;
    }
    const parent = path.dirname(/* turbopackIgnore: true */ current);
    if (parent === current) return path.resolve(/* turbopackIgnore: true */ process.cwd());
    current = parent;
  }
}

export function ledgerPath(): string {
  const configured = process.env.PRAMANA_LEDGER_PATH;
  if (configured) return path.resolve(/* turbopackIgnore: true */ process.cwd(), configured);
  return path.join(/* turbopackIgnore: true */ projectRoot(), "pramana_ledger.sqlite");
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

export function hasColumn(db: DatabaseSync, table: string, column: string): boolean {
  // PRAGMA cannot take bound parameters; callers pass literal table names only.
  const rows = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
  return rows.some((row) => row.name === column);
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
