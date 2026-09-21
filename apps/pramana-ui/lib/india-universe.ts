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
  segments?: string[];
  currency: string;
  examples: string[];
  mode: IndiaCoverageMode;
  detail: string;
};

/**
 * A declared research universe, not an order list. Generic futures names are
 * intentionally shown as candidates until an exact broker contract is bound.
 *
 * The list follows the major Indian exchange products and broker segments. A
 * group is marked observed only when a matching row is present in the current
 * read-only snapshot; declaration alone never widens the pilot order gate.
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
    assetClasses: ["EQUITY"],
    currency: "INR",
    examples: ["SENSEX", "RELIANCE", "TCS"],
    mode: "research_only",
    detail: "The collector probes a small BSE watchlist for read-only evidence; the pilot gate remains NSE-only and the wider BSE universe still needs explicit instrument mapping.",
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
    id: "bse-index-etf",
    label: "BSE indices and ETFs",
    exchange: "BSE",
    assetClasses: ["INDEX", "ETF"],
    currency: "INR",
    examples: ["SENSEX", "BANKEX", "BSE ETFs"],
    mode: "research_only",
    detail: "BSE index and ETF observations need a separately mapped broker instrument; no synthetic index values are created.",
  },
  {
    id: "nse-debt",
    label: "NSE debt and government securities",
    exchange: "NSE",
    assetClasses: ["BOND", "DEBT"],
    segments: ["DEBT"],
    currency: "INR",
    examples: ["G-SEC", "T-BILLS", "CORPORATE BONDS"],
    mode: "research_only",
    detail: "Debt, treasury and corporate-bond instruments are catalogued for research; pricing, accrued interest and settlement conventions still need a licensed source.",
  },
  {
    id: "bse-debt",
    label: "BSE debt and government securities",
    exchange: "BSE",
    assetClasses: ["BOND", "DEBT"],
    segments: ["DEBT"],
    currency: "INR",
    examples: ["G-SEC", "T-BILLS", "CORPORATE BONDS"],
    mode: "research_only",
    detail: "BSE debt and fixed-income products are research-only until a provider supplies clean prices, accrued interest and settlement metadata.",
  },
  {
    id: "nse-funds",
    label: "NSE mutual funds and SGBs",
    exchange: "NSE",
    assetClasses: ["FUND", "BOND"],
    segments: ["FUNDS"],
    currency: "INR",
    examples: ["Mutual funds", "Sovereign gold bonds"],
    mode: "research_only",
    detail: "Mutual-fund NAVs and sovereign-gold-bond records are not treated as live quotes; source, valuation date and settlement must be retained.",
  },
  {
    id: "bse-funds",
    label: "BSE mutual funds and SGBs",
    exchange: "BSE",
    assetClasses: ["FUND", "BOND"],
    segments: ["FUNDS"],
    currency: "INR",
    examples: ["Mutual funds", "Sovereign gold bonds"],
    mode: "research_only",
    detail: "BSE fund and sovereign-gold-bond records need a dated NAV or trade source and are not eligible for the pilot order path.",
  },
  {
    id: "nse-sme-ipo",
    label: "NSE IPO and SME listings",
    exchange: "NSE",
    assetClasses: ["EQUITY", "IPO"],
    segments: ["IPO"],
    currency: "INR",
    examples: ["IPO", "NSE Emerge"],
    mode: "research_only",
    detail: "IPO and SME listings require issue, allotment, listing-date and liquidity evidence before they can enter a research watchlist.",
  },
  {
    id: "bse-sme-ipo",
    label: "BSE IPO and SME listings",
    exchange: "BSE",
    assetClasses: ["EQUITY", "IPO"],
    segments: ["IPO"],
    currency: "INR",
    examples: ["IPO", "BSE SME"],
    mode: "research_only",
    detail: "BSE SME and IPO records are declared so the dashboard does not imply that the regular cash list is exhaustive; no order route is enabled.",
  },
  {
    id: "nse-slb",
    label: "NSE securities lending",
    exchange: "NSE",
    assetClasses: ["SLB", "EQUITY"],
    segments: ["SLB"],
    currency: "INR",
    examples: ["Securities lending and borrowing"],
    mode: "research_only",
    detail: "SLB has different collateral, tenure and settlement rules; it is catalogued for evidence only and cannot be sent through the cash-equity pilot.",
  },
  {
    id: "nse-interest-rate",
    label: "NSE interest-rate derivatives",
    exchange: "NFO",
    assetClasses: ["FUTURE", "OPTION"],
    segments: ["IRD"],
    currency: "INR",
    examples: ["Interest-rate futures", "Interest-rate options"],
    mode: "requires_contract",
    detail: "Bind an exact NFO contract, expiry, strike where applicable, lot, tick, product and margin schedule before any paper test.",
  },
  {
    id: "mcx-metals",
    label: "MCX metals",
    exchange: "MCX",
    assetClasses: ["METAL", "FUTURE", "OPTION"],
    segments: ["METALS"],
    currency: "INR",
    examples: ["GOLD", "SILVER", "COPPER", "ALUMINIUM", "ZINC", "NICKEL", "LEAD"],
    mode: "requires_contract",
    detail: "Research candidates only. An exact expiry, lot size, tick size, product and Zerodha instrument token are required before paper execution.",
  },
  {
    id: "mcx-energy",
    label: "MCX energy",
    exchange: "MCX",
    assetClasses: ["COMMODITY", "FUTURE", "OPTION"],
    segments: ["ENERGY"],
    currency: "INR",
    examples: ["CRUDEOIL", "NATURALGAS"],
    mode: "requires_contract",
    detail: "Crude and gas are not represented by a generic spot label; bind the currently listed futures or option contract and its session before use.",
  },
  {
    id: "mcx-agri",
    label: "MCX agricultural commodities",
    exchange: "MCX",
    assetClasses: ["COMMODITY", "FUTURE", "OPTION"],
    segments: ["AGRI"],
    currency: "INR",
    examples: ["COTTON", "MENTHAOIL", "GUARSEED"],
    mode: "requires_contract",
    detail: "MCX agriculture contracts are research candidates only; contract month, quality unit, lot, tick, delivery and session must be captured.",
  },
  {
    id: "cds-currency",
    label: "NSE currency derivatives (CDS)",
    exchange: "CDS",
    assetClasses: ["FX", "FUTURE", "OPTION"],
    segments: ["CDS"],
    currency: "INR",
    examples: ["USDINR", "EURINR", "GBPINR", "JPYINR"],
    mode: "requires_contract",
    detail: "Currency futures/options need the exact contract, expiry and lot specification plus a CDS-enabled account.",
  },
  {
    id: "bcd-currency",
    label: "BSE currency derivatives",
    exchange: "BCD",
    assetClasses: ["FX", "FUTURE", "OPTION"],
    segments: ["BCD"],
    currency: "INR",
    examples: ["USDINR", "EURINR", "GBPINR", "JPYINR"],
    mode: "requires_contract",
    detail: "BSE currency contracts require an exact BCD symbol, expiry, lot, tick, product and enabled broker segment; generic pair names are rejected.",
  },
  {
    id: "nse-derivatives",
    label: "NSE equity futures and options",
    exchange: "NFO",
    assetClasses: ["OPTION", "FUTURE"],
    segments: ["FNO"],
    currency: "INR",
    examples: ["NIFTY futures/options", "BANKNIFTY futures/options", "FINNIFTY futures/options"],
    mode: "requires_contract",
    detail: "Derivatives remain research-only until the exact NFO symbol, expiry, strike, option type, lot, tick, margin and risk controls are qualified.",
  },
  {
    id: "bse-derivatives",
    label: "BSE futures and options",
    exchange: "BFO",
    assetClasses: ["OPTION", "FUTURE"],
    segments: ["FNO"],
    currency: "INR",
    examples: ["SENSEX futures/options", "BANKEX futures/options"],
    mode: "requires_contract",
    detail: "BSE derivatives need an exact BFO contract and option metadata; the pilot gate does not widen when a row is observed.",
  },
  {
    id: "nse-commodity",
    label: "NSE commodity derivatives",
    exchange: "NSE",
    assetClasses: ["COMMODITY", "FUTURE", "OPTION"],
    segments: ["COMMODITY"],
    currency: "INR",
    examples: ["NSE commodity futures", "NSE commodity options"],
    mode: "requires_contract",
    detail: "NSE commodity products are listed as a separate research segment; exact contract, expiry, strike and settlement metadata are mandatory.",
  },
  {
    id: "bse-commodity",
    label: "BSE commodity derivatives",
    exchange: "BFO",
    assetClasses: ["COMMODITY", "FUTURE", "OPTION"],
    segments: ["COMMODITY"],
    currency: "INR",
    examples: ["BSE commodity futures", "BSE commodity options"],
    mode: "requires_contract",
    detail: "BSE commodity products share the derivative risk boundary: no generic metal or energy label can be queried as a contract.",
  },
  {
    id: "ncdex-agri",
    label: "NCDEX agricultural commodities",
    exchange: "NCDEX",
    assetClasses: ["COMMODITY", "FUTURE", "OPTION"],
    segments: ["AGRI"],
    currency: "INR",
    examples: ["GUARSEED", "COTTON", "SOYBEAN"],
    mode: "requires_contract",
    detail: "Shown as a future coverage area; no NCDEX feed or paper contract is enabled in this deployment.",
  },
  {
    id: "msei-cash",
    label: "MSEI cash equities",
    exchange: "MSEI",
    assetClasses: ["EQUITY", "INDEX"],
    currency: "INR",
    examples: ["MSEI equity market"],
    mode: "research_only",
    detail: "MSEI is declared as a separate venue so coverage is explicit; this deployment has no MSEI quote adapter or pilot order route.",
  },
  {
    id: "msei-derivatives",
    label: "MSEI equity and currency derivatives",
    exchange: "MSEI",
    assetClasses: ["OPTION", "FUTURE", "FX"],
    segments: ["DERIVATIVE"],
    currency: "INR",
    examples: ["Equity derivatives", "Currency derivatives"],
    mode: "requires_contract",
    detail: "MSEI derivatives need a venue-specific contract master and entitlement; no generic symbol is considered evidence of a listed contract.",
  },
  {
    id: "gift-ifsc",
    label: "GIFT City IFSC venues",
    exchange: "IFSC",
    assetClasses: ["EQUITY", "ETF", "FUTURE", "OPTION", "FX", "BOND"],
    segments: ["IFSC"],
    currency: "INR",
    examples: ["NSE IX", "India INX", "GIFT NIFTY"],
    mode: "research_only",
    detail: "IFSC products use distinct venue, currency, tax and session rules. They remain research-only until a supported provider and account entitlement are configured.",
  },
  {
    id: "gift-ifsc-usd",
    label: "GIFT City IFSC (USD products)",
    exchange: "IFSC",
    assetClasses: ["EQUITY", "ETF", "FUTURE", "OPTION", "FX", "BOND"],
    segments: ["IFSC"],
    currency: "USD",
    examples: ["NSE IX", "India INX", "GIFT NIFTY"],
    mode: "research_only",
    detail: "Some IFSC instruments quote in USD. They need a separate provider, FX treatment, tax and session review before paper research.",
  },
  {
    id: "reit-invit",
    label: "Indian REITs and InvITs",
    exchange: "NSE",
    assetClasses: ["REIT", "INVIT"],
    segments: ["REIT"],
    currency: "INR",
    examples: ["Listed REITs", "Listed InvITs"],
    mode: "research_only",
    detail: "REIT and InvIT units have distribution and valuation fields that ordinary equity rows do not capture; they are observation-only here.",
  },
];

export type IndiaCoverage = {
  scope: string;
  paperOnly: true;
  groups: (IndiaCoverageGroup & { status: "observed" | "planned" })[];
  disclaimer: string;
  aiContext: string;
};

function rowInGroup(group: IndiaCoverageGroup, row: MarketRow): boolean {
  const instrument = row.instrument;
  return instrument !== undefined
    && instrument.market === "INDIA"
    && instrument.exchange === group.exchange
    && instrument.currency === group.currency
    && group.assetClasses.includes(instrument.assetClass)
    && (!group.segments || group.segments.includes(instrument.segment || ""));
}

/**
 * Coverage as the collector actually has it. A group with collector rows lists
 * those symbols in collector order, so the tile shows the watchlist that is
 * really polled (the compose default list plus PRAMANA_MARKET_EXTRA_INSTRUMENTS_JSON)
 * rather than the declared examples; a group with no rows keeps its examples as
 * the planned universe. A row that failed this cycle still names a polled symbol,
 * but only a current quote makes the group observed.
 */
export function indiaCoverage(rows: MarketRow[]): IndiaCoverage {
  const groups = INDIA_COVERAGE_GROUPS.map((group) => {
    const polled: string[] = [];
    let observed = false;
    for (const row of rows) {
      if (!rowInGroup(group, row)) continue;
      if (!polled.includes(row.symbol)) polled.push(row.symbol);
      if (row.available === true) observed = true;
    }
    return {
      ...group,
      status: observed ? "observed" as const : "planned" as const,
      examples: polled.length ? polled : group.examples,
    };
  });
  return {
    scope: "India multi-asset observation and paper-research universe",
    paperOnly: true,
    groups,
    disclaimer: "Only groups marked observed have current collector rows. Planned groups are not executable: bind an exact broker instrument, contract and venue entitlement, then complete a separate paper qualification before widening the pilot gate.",
    aiContext: "Atlas receives this catalog and the current observed rows as prompt context. It does not modify model weights or certify a strategy.",
  };
}
