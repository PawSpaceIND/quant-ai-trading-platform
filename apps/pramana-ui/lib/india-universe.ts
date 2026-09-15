import type { MarketRow } from "./market";

export type IndiaCoverageMode =
  | "observed"
  | "research_only"
  | "requires_contract";

export type IndiaCoverageGroup = {
  id: string;
  label: string;
  exchange: string;
  assetClasses: string[];
  currency: string;
  examples: string[];
  mode: IndiaCoverageMode;
  detail: string;
};

/**
 * A declared research universe, not an order list. Generic futures names are
 * intentionally shown as candidates until an exact broker contract is bound.
 */
export const INDIA_COVERAGE_GROUPS: IndiaCoverageGroup[] = [
  {
    id: "nse-cash",
    label: "NSE cash equities",
    exchange: "NSE",
    assetClasses: ["EQUITY"],
    currency: "INR",
    examples: ["INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "ITC", "LT", "TATASTEEL", "HINDALCO"],
    mode: "observed",
    detail: "Read-only quotes are collected for the configured NSE watchlist; the pilot gate permits paper entries only here.",
  },
  {
    id: "bse-cash",
    label: "BSE cash equities",
    exchange: "BSE",
    assetClasses: ["EQUITY", "INDEX"],
    currency: "INR",
    examples: ["SENSEX", "RELIANCE", "TCS"],
    mode: "research_only",
    detail: "A wider Indian equity venue is declared for research, but this deployment has no BSE collector row by default and the pilot gate remains NSE-only.",
  },
  {
    id: "nse-index-etf",
    label: "NSE indices and ETFs",
    exchange: "NSE",
    assetClasses: ["INDEX", "ETF"],
    currency: "INR",
    examples: ["NIFTY 50", "NIFTY BANK", "GOLDBEES", "SILVERBEES"],
    mode: "observed",
    detail: "Indices are observation-only; ETFs can be included in the existing NSE paper scope when explicitly configured.",
  },
  {
    id: "mcx-metals",
    label: "MCX metals",
    exchange: "MCX",
    assetClasses: ["METAL", "FUTURE"],
    currency: "INR",
    examples: ["GOLD", "SILVER", "COPPER", "ALUMINIUM", "ZINC", "NICKEL", "LEAD"],
    mode: "requires_contract",
    detail: "Research candidates only. An exact expiry, lot size, tick size, product and Zerodha instrument token are required before paper execution.",
  },
  {
    id: "mcx-energy",
    label: "MCX energy",
    exchange: "MCX",
    assetClasses: ["COMMODITY", "FUTURE"],
    currency: "INR",
    examples: ["CRUDEOIL", "NATURALGAS"],
    mode: "requires_contract",
    detail: "Crude and gas are not represented by a generic spot label; bind the currently listed futures contract and its session before use.",
  },
  {
    id: "cds-currency",
    label: "Currency derivatives (CDS)",
    exchange: "CDS",
    assetClasses: ["FX", "FUTURE"],
    currency: "INR",
    examples: ["USDINR", "EURINR", "GBPINR", "JPYINR"],
    mode: "requires_contract",
    detail: "Currency futures/options need the exact contract, expiry and lot specification plus a CDS-enabled account.",
  },
  {
    id: "nse-derivatives",
    label: "NSE futures and options",
    exchange: "NSE",
    assetClasses: ["OPTION", "FUTURE"],
    currency: "INR",
    examples: ["NIFTY futures/options", "BANKNIFTY futures/options"],
    mode: "requires_contract",
    detail: "Derivatives remain research-only until expiry, strike, lot, margin and risk controls are qualified.",
  },
  {
    id: "ncdex-agri",
    label: "NCDEX agricultural commodities",
    exchange: "NCDEX",
    assetClasses: ["COMMODITY", "FUTURE"],
    currency: "INR",
    examples: ["GUARSEED", "COTTON", "SOYBEAN"],
    mode: "requires_contract",
    detail: "Shown as a future coverage area; no NCDEX feed or paper contract is enabled in this deployment.",
  },
];

export type IndiaCoverage = {
  scope: string;
  paperOnly: true;
  groups: (IndiaCoverageGroup & { status: "observed" | "planned" })[];
  disclaimer: string;
  aiContext: string;
};

export function indiaCoverage(rows: MarketRow[]): IndiaCoverage {
  const groups = INDIA_COVERAGE_GROUPS.map((group) => {
    const observed = rows.some((row) => {
      const instrument = row.instrument;
      return row.available === true
        && instrument !== undefined
        && instrument.market === "INDIA"
        && instrument.exchange === group.exchange
        && instrument.currency === group.currency
        && group.assetClasses.includes(instrument.assetClass);
    });
    return { ...group, status: observed ? "observed" as const : "planned" as const };
  });
  return {
    scope: "India multi-asset observation and paper-research universe",
    paperOnly: true,
    groups,
    disclaimer: "Only groups marked observed have current collector rows. Planned groups are not executable: bind an exact broker contract and complete a separate paper qualification before widening the pilot gate.",
    aiContext: "Atlas receives this catalog and the current observed rows as prompt context. It does not modify model weights or certify a strategy.",
  };
}
