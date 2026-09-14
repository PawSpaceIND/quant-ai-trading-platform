import type { ResearchReport } from "./research";
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
  equityCurve: { timestamp: string; equity: number }[];
  updatedAt: string | null;
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
  };
};
export type Workspace = {
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
