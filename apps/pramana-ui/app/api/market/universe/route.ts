import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

const clean = (value: unknown, max = 80) =>
  typeof value === "string" && value.length <= max ? value : undefined;
const boundedInt = (value: string | null, fallback: number, max: number) => {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? Math.min(parsed, max) : fallback;
};

export async function GET(request: NextRequest) {
  const configured = process.env.PRAMANA_INSTRUMENT_UNIVERSE_RECORDS ||
    "/data/instrument-universe.normalized.json";
  const limit = Math.max(1, boundedInt(request.nextUrl.searchParams.get("limit"), 100, 500));
  const offset = boundedInt(request.nextUrl.searchParams.get("offset"), 0, 100000);
  const q = (request.nextUrl.searchParams.get("q") || "").trim().toUpperCase().slice(0, 80);
  const exchange = (request.nextUrl.searchParams.get("exchange") || "").trim().toUpperCase();
  const assetClass = (request.nextUrl.searchParams.get("assetClass") || "").trim().toUpperCase();
  const segment = (request.nextUrl.searchParams.get("segment") || "").trim().toUpperCase();
  const expiry = (request.nextUrl.searchParams.get("expiry") || "").trim();
  const optionType = (request.nextUrl.searchParams.get("optionType") || "").trim().toUpperCase();
  try {
    const raw = JSON.parse(await fs.readFile(path.resolve(/* turbopackIgnore: true */ configured), "utf8")) as {schema?: unknown; fetchedAt?: unknown; rows?: unknown};
    if (raw.schema !== "pramana.instrument_universe.v1" || !Array.isArray(raw.rows)) throw new Error("invalid universe");
    const rows = raw.rows.filter((item): item is Record<string, unknown> => {
      if (!item || typeof item !== "object") return false;
      const row = item as Record<string, unknown>;
      if (typeof row.exchange !== "string" || typeof row.symbol !== "string") return false;
      return (!q || row.symbol.toUpperCase().includes(q) || String(row.underlying || "").toUpperCase().includes(q))
        && (!exchange || row.exchange === exchange)
        && (!assetClass || row.assetClass === assetClass)
        && (!segment || row.segment === segment)
        && (!expiry || row.expiry === expiry)
        && (!optionType || row.optionType === optionType);
    });
    const page = rows.slice(offset, offset + limit).map((row) => ({
      symbol: clean(row.symbol),
      contract: clean(row.contract),
      exchange: clean(row.exchange, 20),
      market: clean(row.market, 20),
      currency: clean(row.currency, 10),
      assetClass: clean(row.assetClass, 30),
      segment: clean(row.segment, 40),
      instrumentType: clean(row.instrumentType, 20),
      underlying: clean(row.underlying),
      expiry: clean(row.expiry, 20),
      optionType: clean(row.optionType, 4),
      strike: typeof row.strike === "number" && Number.isFinite(row.strike) ? row.strike : undefined,
      lotSize: typeof row.lotSize === "number" && Number.isFinite(row.lotSize) ? row.lotSize : undefined,
      tickSize: typeof row.tickSize === "number" && Number.isFinite(row.tickSize) ? row.tickSize : undefined,
      providerInstrumentId: clean(row.providerInstrumentId, 100),
      providerExchangeToken: clean(row.providerExchangeToken, 100),
      product: clean(row.product, 40),
      shortability: clean(row.shortability, 80),
    }));
    return NextResponse.json({schema: raw.schema, fetchedAt: raw.fetchedAt, total: rows.length, offset, limit, rows: page}, {headers: {"Cache-Control": "no-store"}});
  } catch {
    return NextResponse.json({schema: "pramana.instrument_universe.v1", status: "unavailable", total: 0, rows: [], note: "The broker instrument master has not been captured."}, {status: 200, headers: {"Cache-Control": "no-store"}});
  }
}
