import type { DatabaseSync } from "node:sqlite";

function observationInstant(v: unknown) {
  if (typeof v !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(v))
    throw new Error("Observation timezone required");
  const at=Date.parse(v),zone=v.endsWith("Z")?null:v.slice(-6);
  const offset=zone?(zone[0]==="-"?-1:1)*(Number(zone.slice(1,3))*60+Number(zone.slice(4))):0;
  if (!Number.isFinite(at) || v.startsWith("0000-") || new Date(at+offset*60000).toISOString().slice(0,19)!==v.slice(0,19))
    throw new Error("Invalid observation date");
  return at;
}

/** Stream retained rows without materializing the account's whole payload history. */
export function* observationHistory(db: DatabaseSync, tenant: string) {
  const rows = db.prepare(
    "SELECT timestamp,payload FROM paper_live_valuations WHERE tenant_id=? ORDER BY timestamp",
  ).iterate(tenant);
  for (const row of rows) {
    if (typeof row.payload !== "string" || Buffer.byteLength(row.payload) > 1_000_000)
      throw new Error("Invalid observation payload");
    const p = JSON.parse(row.payload);
    if (!p || typeof p !== "object" || Array.isArray(p))
      throw new Error("Invalid observation payload");
    if (typeof row.timestamp !== "string") throw new Error("Invalid observation bucket");
    const timestamp = observationInstant(p.updatedAt), bucket = observationInstant(row.timestamp);
    if (!Number.isFinite(timestamp) || !Number.isFinite(bucket) || bucket % 60000 !== 0 ||
        row.timestamp.slice(17,19) !== "00" || /\.[0-9]*[1-9][0-9]*(?:Z|[+-])/.test(row.timestamp) ||
        Math.floor(timestamp / 60000) * 60000 !== bucket)
      throw new Error("Observation minute does not match its stored bucket");
    yield { p, timestamp, bucket };
  }
}
