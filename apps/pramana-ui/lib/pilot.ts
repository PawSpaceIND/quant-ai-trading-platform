import { hasTable, openLedger, tenantId } from "./db";
import type {DatabaseSync} from "node:sqlite";
import { observationHistory } from "./observation-history";
import { agePortfolio, ageRuntime } from "./freshness";
export type LivePortfolio = {
  ledgerId?: number;
  status: string;
  tenantId: string;
  currency: string;
  markMode: string;
  markDisclaimer: string;
  cash: number;
  totalEquity: number;
  startingCapital: number;
  realizedPnl: number;
  unrealizedPnl: number;
  dailyPnl: number;
  highWaterMark: number;
  drawdown: number;
  updatedAt: string;
  allMarksFresh: boolean;
  sessionDate?: string;
  previousSessionDate?: string;
  qualifyingSession?: boolean;
  holdings: Array<{
    symbol: string;
    market: string;
    assetClass: string;
    currency: string;
    quantity: number;
    averageEntry: number;
    markPrice: number;
    marketValue: number;
    unrealizedPnl: number;
    markSource: string;
    markTimestamp: string | null;
    fresh: boolean;
    stopPrice: number | null;
    takeProfitPrice: number | null;
  }>;
  equityCurve: Array<{ timestamp: string; equity: number | null }>;
};
export type StrategyEpisodeEvidence = {
  schema: string; status: string; strategySha256: string; sourceSha256: string; evidenceSha256: string;
  generatedAt: string; coverageStartedAt: string; ledgerId: number; unresolvedEpisodes: number; foreignOpenEpisodes: number;
  unlinkedAccountCompletedTrades: number; incompatibleSessionDates: string[];
  summary: {completedTrades: number; openEpisodes: number; netPnl: string; expectancy: string | null;
    profitFactor: string | null; profitFactorState: string; winRate: string | null; closedCashFees: string};
};
/**
 * One sweep of the protective-exit engine, symbol by symbol. `protectionCoverage` says a
 * stop is stored; this says whether the engine could act on it. `sweptAt` is nullable on
 * purpose: empty lists mean "swept and clean" only when a sweep actually happened.
 */
export type ProtectionSweep = {
  schema: string; tenantId: string; checkedAt: string; sweptAt: string | null;
  /**
   * `unpricedSince` runs at every hour. The halt counts only session time: `pricesExpected`
   * says whether the symbol's market is open, `haltClockSince` when that clock started.
   * Both are absent from an engine that cannot state them, and absent never means closed.
   */
  unprotected: Array<{symbol: string; unpricedSince: string | null;
    pricesExpected?: boolean; haltClockSince?: string | null}>;
  rebased: string[];
  haltAfterSeconds: number | null;
  gapMonitor: {armed: boolean; unresolved: Array<{
    symbol: string; verdict: string; venue: string; previousMark: string; currentMark: string;
    stepFraction: string; nearestAction: string; firstSeenAt: string; lastAlertAt: string;
    haltsAt: string | null;
  }>};
};
/**
 * One opt-in entry control. `armed` is the field that matters: every gate here is silent
 * when it is off and silent when it is on and content, and those are not the same fact.
 * `records` counts what the operator supplied, so armed-with-an-empty-file reads as its
 * own third state.
 */
export type RiskGate = {
  id: string; setting: string; armed: boolean;
  records?: number | null; groups?: number | null; limit?: number | null;
  observed?: number | null; observedUnavailable?: string | null;
  closingWindowSeconds?: number | null;
  blackouts?: Array<{category: string; symbol: string | null}>;
};
export type RiskGates = {schema: string; tenantId: string; checkedAt: string; gates: RiskGate[]};
export type Runtime = {
  protectionSweep?: ProtectionSweep | null;
  riskGates?: RiskGates | null;
  /** The probe budget the running engine applies; null or absent when it published none. */
  exploration?: import("./probe-budget-model").Exploration | null;
  marketDataIntegrity?: {schema: string; accepted: number; rejected: Record<string, number>;
    lastRejection: {reason: string; symbol: string; observedAt: string | null; receivedAt: string} | null; scope: string};
  valuation?: {status: string; reason?: string; checkedAt: string; ledgerId: number};
  protectionCoverage?: {
    schema: string; tenantId: string; status: string; checkedAt: string; ledgerId: number;
    positionCount: number; coveredCount: number; missingStopCount: number; invalidPositionCount: number;
    issueCount: number; issues: Array<{key: string; code: string}>; scope: string;
  } | null;
  strategyEvidence?: StrategyEpisodeEvidence | null;
  strategyManifest?: {status: string; sha256?: string; bootSha256?: string; sourceSha256?: string;
    releaseRevision?: string; checkedAt: string; sourceCheckAgeSeconds?: number; issues: string[]} | null;
  tradeEvidence?: {status: string; currency?: string; ledgerId: number; fillCount: number; generatedAt: string; sourceSha256: string; reason?: string; summary?: {
    completedTrades: number; openEpisodes: number; wins: number; losses: number; breakeven: number;
    netPnl: string; expectancy: string | null; winRate: string | null; profitFactor: string | null;
    profitFactorState: string; closedCashFees: string; openCashFees: string;
  }} | null;
  reconciliation?: {status: string; checkedAt: string; ledgerId: number; issueCount: number; scope: string} | null;
  status: string;
  mode: string;
  runtimeEvidenceIssue?: string;
  updatedAt?: string;
  halted?: boolean;
  haltReason?: string;
  watchlist?: {
    symbol: string;
    currency: string;
    market: string;
    assetClass: string;
    exchange: string;
    fresh: boolean;
    tickTimestamp?: string | null;
    tickAgeSeconds?: number | null;
    freshnessReason?: string;
    regimeContext?: unknown;
  }[];
  lastAnalysisAt?: string;
  providers?: Record<string, string>;
  limits?: {
    dailyLoss: number;
    drawdown: number;
    grossExposure?: number;
    maxPositions: number;
  };
};
export function readLivePortfolio(connection?: DatabaseSync): LivePortfolio | null {
  const db = connection ?? openLedger();
  if (!db) return null;
  try {
    if (!hasTable(db, "paper_live_valuations")) return null;
    const rows = db
      .prepare(
        "SELECT ledger_id,payload FROM paper_live_valuations WHERE tenant_id=? ORDER BY timestamp DESC LIMIT 10000",
      )
      .all(tenantId) as { ledger_id: number; payload: string }[];
    if (!rows.length) return null;
    const latest = JSON.parse(rows[0].payload) as LivePortfolio;
    const head = db
      .prepare(
        "SELECT COALESCE(MAX(id),0) AS id FROM paper_ledger WHERE tenant_id=?",
      )
      .get(tenantId) as { id: number };
    const age = Date.now() - Date.parse(latest.updatedAt);
    const stale =
      age > 30000 ||
      age < -5000 ||
      !Number.isFinite(age) ||
      rows[0].ledger_id !== head.id;
    return agePortfolio({
      ...latest,
      ledgerId: rows[0].ledger_id,
      status: latest.status === "invalid" ? "invalid" : stale ? "stale" : latest.status,
      allMarksFresh: !stale && latest.allMarksFresh,
      markDisclaimer: stale && latest.status !== "invalid"
        ? "Engine snapshot is stale or precedes a newer fill. These are last observed values, not current equity."
        : latest.markDisclaimer,
      equityCurve: rows.reverse().map((r) => {
        const p = JSON.parse(r.payload);
        return { timestamp: p.updatedAt, equity: p.status === "invalid" || !Number.isFinite(p.totalEquity) ? null : p.totalEquity as number };
      }),
    });
  } finally {
    if (!connection) db.close();
  }
}
export function readRuntime(): Runtime {
  const db = openLedger();
  if (!db) return { status: "unavailable", mode: "paper" };
  try {
    if (!hasTable(db, "pilot_runtime"))
      return { status: "unavailable", mode: "paper" };
    const row = db
      .prepare("SELECT CASE WHEN length(CAST(payload AS BLOB))<=1000000 THEN payload ELSE NULL END AS payload FROM pilot_runtime WHERE tenant_id=?")
      .get(tenantId) as { payload: string } | undefined;
    if (!row) return { status: "unavailable", mode: "paper" };
    const data = JSON.parse(row.payload) as Runtime;
    if (!data || typeof data!=="object" || Array.isArray(data) || typeof data.status!=="string" || typeof data.mode!=="string")
      throw new Error("Invalid runtime evidence");
    return ageRuntime(data);
  } catch {
    return {status:"invalid",mode:"paper",runtimeEvidenceIssue:"Engine evidence is unreadable, malformed or exceeds the supported payload bound."};
  } finally {
    db.close();
  }
}
type CapitalFlow = { at: number; before: number; after: number };

// Recorded capital contributions, oldest first. Each must continue from the one before it,
// so a changed starting capital in the valuations is explained by exactly this schedule or
// not at all. Contributions are refused during NSE hours, so no in-session observation can
// sit on both sides of one.
function capitalFlows(db: DatabaseSync): CapitalFlow[] {
  if (!hasTable(db, "paper_capital_contributions")) return [];
  const rows = db
    .prepare("SELECT contributed_at, capital_before, capital_after FROM paper_capital_contributions WHERE tenant_id=? ORDER BY contributed_at LIMIT 1001")
    .all(tenantId) as { contributed_at: string; capital_before: string; capital_after: string }[];
  if (rows.length > 1000) throw new Error("Capital contribution history exceeds bounds");
  const flows = rows.map((r) => ({ at: Date.parse(r.contributed_at), before: Number(r.capital_before), after: Number(r.capital_after) }));
  flows.forEach((f, i) => {
    if (!Number.isFinite(f.at) || !Number.isFinite(f.before) || !Number.isFinite(f.after) || f.before <= 0 || f.after <= f.before)
      throw new Error("Invalid capital contribution record");
    if (i && (f.before !== flows[i - 1].after || f.at < flows[i - 1].at)) throw new Error("Discontinuous capital contributions");
  });
  return flows;
}

function capitalAt(flows: CapitalFlow[], at: number): number {
  let capital = flows[0].before;
  for (const f of flows) if (f.at <= at) capital = f.after;
  return capital;
}

function contributedBetween(flows: CapitalFlow[], from: number, to: number): number {
  return flows.reduce((sum, f) => (f.at > from && f.at <= to ? sum + f.after - f.before : sum), 0);
}

export function performance() {
  const db = openLedger();
  if (!db)
    return {
      status: "insufficient_data",
      daily: [],
      days: 0,
      sharpe: null,
      sortino: null,
      netReturn: null,
      source: "actual_paper_equity",
    };
  try {
    if (!hasTable(db, "paper_live_valuations"))
      return {
        status: "insufficient_data",
        daily: [],
        days: 0,
        sharpe: null,
        sortino: null,
        netReturn: null,
        source: "actual_paper_equity",
      };
    const days = new Map<
      string,
      {
        date: string;
        equity: number;
        minutes: Set<number>;
        lastMinute: number;
        lastAt: number;
        previousSessionDate?: string;
      }
    >();
    let initial: number | undefined;
    let firstAt: number | undefined;
    const flows = capitalFlows(db);
    const invalidDays = new Set<string>();
    const today = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
    for (const {p,timestamp,bucket} of observationHistory(db,tenantId)) {
      if (p.status === "invalid" && p.sessionDate) invalidDays.add(p.sessionDate);
      if (days.size > 10000 || invalidDays.size > 10000) throw new Error("Observation history exceeds day bounds");
      if (
        p.allMarksFresh !== true ||
        p.qualifyingSession !== true ||
        !p.sessionDate ||
        p.sessionDate >= today ||
        !Number.isFinite(p.totalEquity) ||
        p.totalEquity <= 0
      )
        continue;
      const local = new Date(timestamp + 330 * 60000);
      const lastMinute = local.getUTCHours() * 60 + local.getUTCMinutes();
      if (local.toISOString().slice(0,10) !== p.sessionDate || lastMinute < 555 || lastMinute >= 930) continue;
      if (!Number.isFinite(p.startingCapital) || p.startingCapital <= 0) throw new Error("Invalid starting capital");
      initial ??= p.startingCapital;
      firstAt ??= timestamp;
      const expected = flows.length ? capitalAt(flows, timestamp) : initial;
      if (expected !== p.startingCapital) throw new Error("Observation starting capital changed");
      const previous = days.get(p.sessionDate);
      const minutes = previous?.minutes ?? new Set<number>();
      if (minutes.has(bucket)) invalidDays.add(p.sessionDate);
      minutes.add(bucket);
      if (previous && previous.lastAt > timestamp) continue;
      days.set(p.sessionDate, {
        date: p.sessionDate,
        equity: p.totalEquity,
        minutes,
        lastMinute,
        lastAt: timestamp,
        previousSessionDate: p.previousSessionDate,
      });
    }
    // A full observation needs at least 300 distinct minute buckets, including the
    // final five minutes of the cash session. Partial days never count as burn-in.
    if (days.size > 10000) throw new Error("Observation history exceeds day bounds");
    const kept = [...days.values()].filter(
      (d) => !invalidDays.has(d.date) && d.minutes.size >= 300 && d.lastMinute >= 15 * 60 + 25,
    ).sort((a,b)=>a.date.localeCompare(b.date));
    // Capital added since the previous kept close is a deposit, not a return: it is taken
    // out of the day it lands in, so returns are time-weighted across contributions.
    const deposited = kept.map((d, i) => contributedBetween(flows, i ? kept[i - 1].lastAt : firstAt ?? d.lastAt, d.lastAt));
    const daily = kept.map(({minutes,lastAt: _lastAt,...d})=>({...d,minutes:minutes.size}));
    const returns = daily
      .slice(1)
      .flatMap((r, i) =>
        r.previousSessionDate === daily[i].date
          ? [(r.equity - deposited[i + 1]) / daily[i].equity - 1]
          : [],
      )
      .filter(Number.isFinite);
    const mean = returns.length
      ? returns.reduce((a, b) => a + b, 0) / returns.length
      : 0;
    const sd =
      returns.length > 1
        ? Math.sqrt(
            returns.reduce((a, b) => a + (b - mean) ** 2, 0) /
              (returns.length - 1),
          )
        : 0;
    const downside = returns.length
      ? Math.sqrt(
          returns.reduce((a, b) => a + Math.min(b, 0) ** 2, 0) / returns.length,
        )
      : 0;
    return {
      status: returns.length >= 20 ? "observed" : "insufficient_data",
      days: daily.length,
      daily,
      source: "actual_paper_equity",
      annualization: 252,
      sharpe:
        returns.length >= 20 && sd > 0 ? (mean / sd) * Math.sqrt(252) : null,
      sortino:
        returns.length >= 20 && downside > 0
          ? (mean / downside) * Math.sqrt(252)
          : null,
      netReturn:
        initial && daily.length
          ? daily.reduce((growth, d, i) => growth * ((d.equity - deposited[i]) / (i ? daily[i - 1].equity : initial!)), 1) - 1
          : null,
    };
  } catch {
    return {status:"invalid_observations",daily:[],days:0,sharpe:null,sortino:null,netReturn:null,source:"actual_paper_equity"};
  } finally {
    db.close();
  }
}
