import fs from "node:fs/promises";
import path from "node:path";
import { homedir } from "node:os";
import { indiaCoverage, type IndiaCoverage } from "./india-universe";
export type MarketInstrument = {
  symbol?: string;
  market: string;
  assetClass: string;
  currency: string;
  exchange: string;
  providerInstrumentId?: string;
  providerExchangeToken?: string;
  contract?: string;
  expiry?: string;
  underlying?: string;
  optionType?: string;
  strike?: number;
  product?: string;
  segment?: string;
  lotSize?: number;
  tickSize?: number;
};
export type UniverseInstrument = MarketInstrument & {instrumentType?: string; shortability?: string};
export type InstrumentUniverse = {
  schema: string;
  status: string;
  source?: string;
  fetchedAt?: string;
  cacheAgeSeconds?: number;
  total: number;
  byExchange: Record<string, number>;
  bySegment: Record<string, number>;
  byAssetClass: Record<string, number>;
  byInstrumentType: Record<string, number>;
  expiringContracts: number;
  optionContracts: number;
  futureContracts: number;
  derivativeContracts: number;
  shortSide?: string;
  entitlement?: string;
  sample?: UniverseInstrument[];
};

export type MarketRow = {
  symbol: string;
  available: boolean;
  price?: number;
  change?: number | null;
  volume?: number;
  lastTrade?: string;
  exchangeTimestamp?: string;
  history?: { date: string; close: number }[];
  instrument?: MarketInstrument;
};
export type MarketSnapshot = {
  riskHistory?: unknown;
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
  coverage?: IndiaCoverage;
  instrumentUniverse?: InstrumentUniverse;
};
const cleanCount = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
const cleanCounts = (value: unknown): Record<string, number> => {
  if (!value || typeof value !== "object") return {};
  return Object.fromEntries(Object.entries(value as Record<string, unknown>)
    .filter(([key, item]) => key.length <= 80 && Number.isSafeInteger(item) && (item as number) >= 0)
    .slice(0, 100)
    .map(([key, item]) => [key, item as number]));
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
      .map((r: MarketRow) => {
        const item = r.instrument;
        const instrument = item && typeof item === "object"
          && typeof item.market === "string" && item.market.length <= 40
          && typeof item.assetClass === "string" && item.assetClass.length <= 40
          && typeof item.currency === "string" && item.currency.length <= 10
          && typeof item.exchange === "string" && item.exchange.length <= 20
          ? {
              symbol: typeof item.symbol === "string" ? item.symbol : undefined,
              market: item.market,
              assetClass: item.assetClass,
              currency: item.currency,
              exchange: item.exchange,
              providerInstrumentId: typeof item.providerInstrumentId === "string" ? item.providerInstrumentId : undefined,
              providerExchangeToken: typeof item.providerExchangeToken === "string" ? item.providerExchangeToken : undefined,
              contract: typeof item.contract === "string" ? item.contract : undefined,
              expiry: typeof item.expiry === "string" ? item.expiry : undefined,
              underlying: typeof item.underlying === "string" ? item.underlying : undefined,
              optionType: typeof item.optionType === "string" ? item.optionType : undefined,
              strike: typeof item.strike === "number" && Number.isFinite(item.strike) ? item.strike : undefined,
              product: typeof item.product === "string" ? item.product : undefined,
              segment: typeof item.segment === "string" ? item.segment : undefined,
              lotSize: typeof item.lotSize === "number" && Number.isFinite(item.lotSize) ? item.lotSize : undefined,
              tickSize: typeof item.tickSize === "number" && Number.isFinite(item.tickSize) ? item.tickSize : undefined,
            }
          : undefined;
        return {
          ...r,
          instrument,
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
        };
      });
    const rawUniverse = raw.instrumentUniverse;
    const instrumentUniverse: InstrumentUniverse | undefined = rawUniverse && typeof rawUniverse === "object"
      && typeof rawUniverse.status === "string" && Number.isSafeInteger(rawUniverse.total)
      ? {
          schema: typeof rawUniverse.schema === "string" ? rawUniverse.schema : "pramana.instrument_universe.v1",
          status: rawUniverse.status,
          source: typeof rawUniverse.source === "string" ? rawUniverse.source : undefined,
          fetchedAt: typeof rawUniverse.fetchedAt === "string" ? rawUniverse.fetchedAt : undefined,
          cacheAgeSeconds: typeof rawUniverse.cacheAgeSeconds === "number" && Number.isFinite(rawUniverse.cacheAgeSeconds) ? rawUniverse.cacheAgeSeconds : undefined,
          total: rawUniverse.total,
          byExchange: cleanCounts(rawUniverse.byExchange),
          bySegment: cleanCounts(rawUniverse.bySegment),
          byAssetClass: cleanCounts(rawUniverse.byAssetClass),
          byInstrumentType: cleanCounts(rawUniverse.byInstrumentType),
          expiringContracts: cleanCount(rawUniverse.expiringContracts),
          optionContracts: cleanCount(rawUniverse.optionContracts),
          futureContracts: cleanCount(rawUniverse.futureContracts),
          derivativeContracts: cleanCount(rawUniverse.derivativeContracts),
          shortSide: typeof rawUniverse.shortSide === "string" ? rawUniverse.shortSide : undefined,
          entitlement: typeof rawUniverse.entitlement === "string" ? rawUniverse.entitlement : undefined,
          sample: Array.isArray(rawUniverse.sample) ? rawUniverse.sample.slice(0,100).filter((item: unknown): item is UniverseInstrument => {
            if (!item || typeof item !== "object") return false;
            const value = item as Record<string, unknown>;
            return typeof value.exchange === "string" && typeof value.symbol === "string" && typeof value.assetClass === "string";
          }) : [],
        }
      : undefined;
    return {
      ...raw,
      rows,
      instrumentUniverse,
      coverage: indiaCoverage(rows),
      collectorStale:
        !Number.isFinite(timestamp) || age > 120000 || age < -5000,
    };
  } catch {
    return {
      status: "unavailable",
      rows: [],
      collectorStale: true,
      coverage: indiaCoverage([]),
      note: "No readable market snapshot. Start the market collector.",
    };
  }
}
