import type {CompanyEventsState} from "./company-events";
import type { ResearchReport } from "./research";
import type { ResearchLabState } from "./research-lab";
import type { PortfolioResearchState } from "./research-portfolio";
import type { PaperContributionState } from "./paper-contribution";
import type {HistoricalRiskState} from "./historical-risk";
import type { MarketSnapshot } from "./market";
import type { Runtime } from "./pilot";
export type Holding = {
  symbol: string;
  market: string;
  assetClass: string;
  quantity: number;
  averageEntry: number;
  markPrice: number;
  markSource: string;
  marketValue: number;
  unrealizedPnl: number;
  fresh?: boolean;
  markTimestamp?: string | null;
  stopPrice?: number | null;
  takeProfitPrice?: number | null;
};
export type Portfolio = {
  status: string;
  markMode: string;
  markDisclaimer: string;
  currency?: string;
  cash: number;
  totalEquity: number;
  startingCapital?: number;
  realizedPnl: number;
  unrealizedPnl: number;
  dailyPnl?: number;
  highWaterMark: number;
  drawdown: number;
  holdings: Holding[];
  equityCurve: { timestamp: string; equity: number | null }[];
  updatedAt: string | null;
};
export type DecisionProvenance = {
  mode: string; status: string; provider: string | null; transport: string | null;
  requestedModel: string | null; resolvedModel: string | null;
  requestSha256: string | null; configurationSha256: string | null;
};
export type Intelligence = {
  status: string;
  regime: string;
  consensus: string;
  agents: {
    agentId: string;
    stance: string;
    confidence: number;
    expectedReturn: number;
    expectedRisk: number;
  }[];
  proof: null | {
    subject: string;
    generatedAt: string;
    rationale: string[];
    risk: Record<string, string>;
    stress: Record<string, string>;
    provenance?: DecisionProvenance;
  };
};
export type Workspace = {
  brokerObservation?: import("./broker-observation").BrokerObservationState;
  benchmarkPerformance?: import("./benchmark-comparison").AccountBenchmarkState;
  historicalRisk?: HistoricalRiskState;
  paperContribution?: PaperContributionState;
  researchLab?: ResearchLabState;
  researchPortfolio?: PortfolioResearchState;
  companyEvents?: CompanyEventsState;
  strategyObservation?: {days:number;daily:{date:string;minutes:number}[];source:string};
  research: ResearchReport | null;
  portfolio: Portfolio;
  market: MarketSnapshot;
  runtime: Runtime;
  performance: {
    status: string;
    days: number;
    daily: { date: string; equity: number }[];
    sharpe: number | null;
    sortino: number | null;
    netReturn: number | null;
    source: string;
  };
  intelligence: Intelligence;
  checks: { id: string; title: string; pass: boolean; detail: string }[];
  audit: { action: string; detail: string; at: string }[];
  tenantId: string;
  haltRequested: boolean;
  copilotConfigured: boolean;
  generatedAt: string;
};
export type Trade = {
  orderId: string;
  symbol: string;
  side: string;
  quantity: number;
  fillPrice: number;
  createdAt: string;
  proofStatus: string;
  proof: null | {
    kind?: string;
    provenance?: DecisionProvenance;
    rationale: string[];
    risk: Record<string, string>;
    stress: Record<string, string>;
  };
};
export type Friction = {
  slippage: number;
  spread: number;
  statutoryFees: number;
  byCode: Record<string, number>;
};
