import { hasTable, openLedger, tenantId } from "./db";
export type LivePortfolio = {
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
  equityCurve: Array<{ timestamp: string; equity: number }>;
};
export type Runtime = {
  tradeEvidence?: {status: string; currency?: string; ledgerId: number; fillCount: number; generatedAt: string; sourceSha256: string; reason?: string; summary?: {
    completedTrades: number; openEpisodes: number; wins: number; losses: number; breakeven: number;
    netPnl: string; expectancy: string | null; winRate: string | null; profitFactor: string | null;
    profitFactorState: string; closedCashFees: string; openCashFees: string;
  }} | null;
  reconciliation?: {status: string; checkedAt: string; ledgerId: number; issueCount: number; scope: string} | null;
  status: string;
  mode: string;
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
export function readLivePortfolio(): LivePortfolio | null {
  const db = openLedger();
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
    return {
      ...latest,
      status: stale ? "stale" : latest.status,
      allMarksFresh: !stale && latest.allMarksFresh,
      markDisclaimer: stale
        ? "Engine snapshot is stale or precedes a newer fill. These are last observed values, not current equity."
        : latest.markDisclaimer,
      equityCurve: rows.reverse().map((r) => {
        const p = JSON.parse(r.payload);
        return { timestamp: p.updatedAt, equity: p.totalEquity };
      }),
    };
  } finally {
    db.close();
  }
}
export function readRuntime(): Runtime {
  const db = openLedger();
  if (!db) return { status: "unavailable", mode: "paper" };
  try {
    if (!hasTable(db, "pilot_runtime"))
      return { status: "unavailable", mode: "paper" };
    const row = db
      .prepare("SELECT payload FROM pilot_runtime WHERE tenant_id=?")
      .get(tenantId) as { payload: string } | undefined;
    if (!row) return { status: "unavailable", mode: "paper" };
    const data = JSON.parse(row.payload) as Runtime;
    const age = Date.now() - Date.parse(data.updatedAt || "");
    return {
      ...data,
      status:
        !Number.isFinite(age) || age > 10000 || age < -5000
          ? "stale"
          : data.status,
    };
  } finally {
    db.close();
  }
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
    const rows = db
      .prepare(
        "SELECT payload FROM paper_live_valuations WHERE tenant_id=? ORDER BY timestamp",
      )
      .all(tenantId) as { payload: string }[];
    const days = new Map<
      string,
      {
        date: string;
        equity: number;
        minutes: number;
        lastMinute: number;
        previousSessionDate?: string;
      }
    >();
    let initial: number | undefined;
    const today = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Kolkata",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
    for (const row of rows) {
      const p = JSON.parse(row.payload) as LivePortfolio;
      if (
        !p.allMarksFresh ||
        !p.qualifyingSession ||
        !p.sessionDate ||
        p.sessionDate >= today ||
        !Number.isFinite(p.totalEquity) ||
        p.totalEquity <= 0
      )
        continue;
      initial ??= p.startingCapital;
      const previous = days.get(p.sessionDate);
      const local = new Date(Date.parse(p.updatedAt) + 330 * 60000);
      const lastMinute = local.getUTCHours() * 60 + local.getUTCMinutes();
      days.set(p.sessionDate, {
        date: p.sessionDate,
        equity: p.totalEquity,
        minutes: (previous?.minutes || 0) + 1,
        lastMinute,
        previousSessionDate: p.previousSessionDate,
      });
    }
    // A full observation needs at least 300 distinct minute buckets, including the
    // final five minutes of the cash session. Partial days never count as burn-in.
    const daily = [...days.values()].filter(
      (d) => d.minutes >= 300 && d.lastMinute >= 15 * 60 + 25,
    );
    const returns = daily
      .slice(1)
      .flatMap((r, i) =>
        r.previousSessionDate === daily[i].date
          ? [r.equity / daily[i].equity - 1]
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
          ? daily[daily.length - 1].equity / initial - 1
          : null,
    };
  } finally {
    db.close();
  }
}
