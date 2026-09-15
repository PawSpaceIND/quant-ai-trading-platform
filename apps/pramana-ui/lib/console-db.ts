import { DatabaseSync } from "node:sqlite";
import fs from "node:fs";
import path from "node:path";
import { ledgerPath, tenantId } from "./db";

export function consoleDb() {
  const target =
    process.env.PRAMANA_CONSOLE_DB ||
    path.join(path.dirname(ledgerPath()), "pilot-console.sqlite");
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(target);
  fs.chmodSync(target, 0o600);
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=2000;
    CREATE TABLE IF NOT EXISTS preferences (tenant TEXT PRIMARY KEY, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, tenant TEXT NOT NULL, prompt TEXT NOT NULL, answer TEXT, status TEXT NOT NULL, model TEXT, usage TEXT, context TEXT, created_at TEXT NOT NULL, error TEXT);
    CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, bucket INTEGER NOT NULL, count INTEGER NOT NULL);`);
  return db;
}
export function audit(action: string, detail: string) {
  const db = consoleDb();
  try {
    db.prepare(
      "INSERT INTO audit(tenant,action,detail,at) VALUES (?,?,?,?)",
    ).run(tenantId, action, detail, new Date().toISOString());
  } finally {
    db.close();
  }
}
export function rateLimit(key: string, maximum: number, seconds = 60) {
  const db = consoleDb();
  const bucket = Math.floor(Date.now() / (seconds * 1000));
  try {
    const row = db
      .prepare(
        `INSERT INTO limits VALUES (?,?,1) ON CONFLICT(key) DO UPDATE SET
    count=CASE WHEN bucket=excluded.bucket THEN count+1 ELSE 1 END, bucket=excluded.bucket RETURNING count`,
      )
      .get(key, bucket) as { count: number };
    return row.count <= maximum;
  } finally {
    db.close();
  }
}
