import { NextRequest, NextResponse } from "next/server";
import { consoleDb, audit } from "@/lib/console-db";
import { tenantId } from "@/lib/db";
import { boundedJson } from "@/lib/auth";
import { readMarket } from "@/lib/market";
export async function GET() {
  const db = consoleDb();
  try {
    const row = db
      .prepare("SELECT payload FROM preferences WHERE tenant=?")
      .get(tenantId) as { payload: string } | undefined;
    return NextResponse.json(row ? JSON.parse(row.payload) : { symbols: [] });
  } finally {
    db.close();
  }
}
export async function PUT(req: NextRequest) {
  try {
    const body = await boundedJson(req, 4000);
    const symbols = body.symbols;
    if (
      !Array.isArray(symbols) ||
      symbols.length > 50 ||
      symbols.some((x) => typeof x !== "string" || x.length > 40)
    )
      throw new Error("Invalid watchlist");
    const unique = [...new Set(symbols)] as string[];
    const market = await readMarket();
    const db = consoleDb();
    try {
      const existing = db
        .prepare("SELECT payload FROM preferences WHERE tenant=?")
        .get(tenantId) as { payload: string } | undefined;
      const known = new Set([
        ...market.rows.map((r) => r.symbol),
        ...(existing ? JSON.parse(existing.payload).symbols : []),
      ]);
      if (unique.some((s) => !known.has(s)))
        throw new Error("Select instruments from available market data");
      db.prepare("INSERT OR REPLACE INTO preferences VALUES (?,?)").run(
        tenantId,
        JSON.stringify({ symbols: unique }),
      );
    } finally {
      db.close();
    }
    audit("watchlist.saved", JSON.stringify(unique));
    return NextResponse.json({ symbols: unique });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "Invalid request" },
      { status: 400 },
    );
  }
}
