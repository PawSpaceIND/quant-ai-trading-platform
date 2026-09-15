import { createHmac, timingSafeEqual, createHash } from "node:crypto";
import { NextRequest } from "next/server";
export const SESSION_COOKIE = "pramana_session";
const lifetime = 8 * 60 * 60 * 1000;
export function authConfigured() {
  return (process.env.PRAMANA_DASHBOARD_SECRET?.length || 0) >= 32;
}
function signature(value: string) {
  return createHmac("sha256", process.env.PRAMANA_DASHBOARD_SECRET || "")
    .update(value)
    .digest("hex");
}
export function constantEqual(a: string, b: string) {
  return timingSafeEqual(
    createHash("sha256").update(a).digest(),
    createHash("sha256").update(b).digest(),
  );
}
export function makeSession() {
  const expires = String(Date.now() + lifetime);
  return `${expires}.${signature(expires)}`;
}
export function validSession(value: string | undefined) {
  if (!authConfigured() || !value || value.length > 160) return false;
  const parts = value.split(".");
  if (parts.length !== 2) return false;
  const [expires, mac] = parts;
  const time = Number(expires);
  return (
    Number.isFinite(time) &&
    time > Date.now() &&
    time <= Date.now() + lifetime &&
    !!mac &&
    constantEqual(mac, signature(expires))
  );
}
export function validOrigin(req: NextRequest) {
  const expected = process.env.PRAMANA_PUBLIC_ORIGIN || new URL(req.url).origin;
  return req.headers.get("origin") === expected;
}
export async function boundedJson(req: Request, maxBytes = 12000) {
  if (!req.headers.get("content-type")?.includes("application/json"))
    throw new Error("JSON required");
  const reader = req.body?.getReader();
  if (!reader) throw new Error("Empty request");
  let size = 0;
  const chunks: Uint8Array[] = [];
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > maxBytes) {
      await reader.cancel();
      throw new Error("Request too large");
    }
    chunks.push(value);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}
