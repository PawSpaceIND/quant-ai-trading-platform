import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";
import { NextResponse } from "next/server";
export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const file = process.env.PRAMANA_MARKET_SNAPSHOT || path.join(homedir(), ".config/pramana/market-monitor.json");
    const data = JSON.parse(await readFile(/* turbopackIgnore: true */ file, "utf8"));
    return NextResponse.json(data, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ status: "unavailable", rows: [], error: "Market collector unavailable" }, { headers: { "Cache-Control": "no-store" } });
  }
}
