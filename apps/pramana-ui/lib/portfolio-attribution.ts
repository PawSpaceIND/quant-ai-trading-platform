import type {Portfolio} from "./types";
import fs from "node:fs";

type Metadata = {schema: "pramana.risk_metadata.v1"; asOf: string; symbols: Record<string, {sector: string; factors: Record<string, number>}>};
export type AttributionState = {
  status: "available" | "unavailable";
  asOf?: string;
  sectors?: {name: string; marketValue: number; weight: number}[];
  factors?: {name: string; exposure: number}[];
  detail: string;
};

function parseMetadata(raw: string | undefined): Metadata | null {
  if (!raw || raw.length > 1_000_000) return null;
  try {
    const value = JSON.parse(raw) as Partial<Metadata>;
    if (value.schema !== "pramana.risk_metadata.v1" || typeof value.asOf !== "string" || !value.asOf || !/[zZ]|[+-]\d{2}:?\d{2}$/.test(value.asOf) || !Number.isFinite(Date.parse(value.asOf)) || !value.symbols || typeof value.symbols !== "object" || Object.keys(value).some((key) => !["schema", "asOf", "symbols"].includes(key))) return null;
    if (Object.keys(value.symbols).length > 500) return null;
    const symbols: Metadata["symbols"] = {};
    for (const [symbol, item] of Object.entries(value.symbols)) {
      if (!item || typeof item !== "object" || typeof item.sector !== "string" || !item.sector.trim() || !item.factors || typeof item.factors !== "object") return null;
      const factors: Record<string, number> = {};
      for (const [name, beta] of Object.entries(item.factors)) {
        if (!name || typeof beta !== "number" || !Number.isFinite(beta) || Math.abs(beta) > 5) return null;
        factors[name] = beta;
      }
      symbols[symbol] = {sector: item.sector.trim(), factors};
    }
    return {schema: value.schema, asOf: value.asOf, symbols};
  } catch { return null; }
}

function configuredMetadata(): string | undefined {
  const inline = process.env.PRAMANA_PORTFOLIO_RISK_METADATA;
  if (inline) return inline;
  const file = process.env.PRAMANA_PORTFOLIO_RISK_METADATA_FILE;
  if (!file) return undefined;
  try { return fs.readFileSync(file, "utf8"); } catch { return undefined; }
}

export function portfolioAttribution(portfolio: Portfolio, raw = configuredMetadata()): AttributionState {
  const metadata = parseMetadata(raw);
  if (!metadata) return {status: "unavailable", detail: "Sector/factor metadata is missing or invalid; attribution is withheld rather than inferred."};
  if (!portfolio.totalEquity || !Number.isFinite(portfolio.totalEquity) || portfolio.totalEquity <= 0 || portfolio.holdings.some((h) => !metadata.symbols[h.symbol] || !Number.isFinite(h.marketValue) || h.marketValue < 0)) {
    return {status: "unavailable", asOf: metadata.asOf, detail: "Every held symbol needs a reviewed sector/factor mapping and a valid current market value."};
  }
  const sectors = new Map<string, number>();
  const factors = new Map<string, number>();
  for (const holding of portfolio.holdings) {
    const item = metadata.symbols[holding.symbol]!;
    sectors.set(item.sector, (sectors.get(item.sector) ?? 0) + holding.marketValue);
    for (const [name, beta] of Object.entries(item.factors)) factors.set(name, (factors.get(name) ?? 0) + beta * holding.marketValue / portfolio.totalEquity);
  }
  return {
    status: "available", asOf: metadata.asOf,
    sectors: [...sectors].sort((a, b) => b[1] - a[1]).map(([name, marketValue]) => ({name, marketValue, weight: marketValue / portfolio.totalEquity})),
    factors: [...factors].sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).map(([name, exposure]) => ({name, exposure})),
    detail: "Exposure uses reviewed metadata and current marked market values; it is not a return attribution or a margin/Greeks model.",
  };
}
