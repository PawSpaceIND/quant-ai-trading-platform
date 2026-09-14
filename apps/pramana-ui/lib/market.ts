import fs from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
export type MarketRow = {
  symbol: string;
  available: boolean;
  price?: number;
  change?: number | null;
  volume?: number;
  lastTrade?: string;
  exchangeTimestamp?: string;
  history?: { date: string; close: number }[];
};
export type MarketSnapshot = {
  status: string;
  fetchedAt?: string;
  session?: string;
  source?: string;
  note?: string;
  rows: MarketRow[];
  providers?: Record<string, string>;
  news?: { headline: string; publishedAt: string; source: string }[];
  collectorStale?: boolean;
  runtime?: Record<string, unknown>;
};
export async function readMarket(): Promise<MarketSnapshot> {
  try {
    const file =
      process.env.PRAMANA_MARKET_SNAPSHOT ||
      path.join(homedir(), ".config/pramana/market-monitor.json");
    const stat = await fs.stat(/* turbopackIgnore: true */ file);
    if (stat.size > 5_000_000) throw new Error("Snapshot too large");
    const raw = JSON.parse(
      await fs.readFile(/* turbopackIgnore: true */ file, "utf8"),
    );
    if (!Array.isArray(raw.rows)) throw new Error("Invalid rows");
    const timestamp = Date.parse(raw.fetchedAt);
    const age = Date.now() - timestamp;
    const rows = raw.rows
      .filter(
        (r: MarketRow) =>
          r &&
          typeof r.symbol === "string" &&
          r.symbol.length <= 40 &&
          typeof r.available === "boolean" &&
          (!r.available || (Number.isFinite(r.price) && r.price! > 0)),
      )
      .slice(0, 500)
      .map((r: MarketRow) => ({
        ...r,
        lastTrade:
          r.lastTrade && !r.lastTrade.startsWith("1970")
            ? r.lastTrade
            : undefined,
        exchangeTimestamp:
          r.exchangeTimestamp && !r.exchangeTimestamp.startsWith("1970")
            ? r.exchangeTimestamp
            : undefined,
        history: (r.history || [])
          .filter((x) => Number.isFinite(x.close) && x.close > 0)
          .slice(-1000),
      }));
    return {
      ...raw,
      rows,
      collectorStale:
        !Number.isFinite(timestamp) || age > 120000 || age < -5000,
    };
  } catch {
    return {
      status: "unavailable",
      rows: [],
      collectorStale: true,
      note: "No readable market snapshot. Start the market collector.",
    };
  }
}
