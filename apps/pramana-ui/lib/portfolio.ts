import { readLivePortfolio } from "./pilot";
import { CostRow, hasColumn, hasTable, LedgerRow, openLedger, tenantId } from "@/lib/db";
import type {DatabaseSync} from "node:sqlite";
import {readPaperContribution, type PaperContributionState} from "./paper-contribution";

type PositionState = { quantity: number; average: number; market: string; assetClass: string };

export type EquityPoint = { timestamp: string; equity: number };

export function readPortfolio() {
  return readPortfolioSnapshot().portfolio;
}

export function readPortfolioSnapshot() {
  const db = openLedger();
  if (!db) return {portfolio: emptyPortfolio("ledger_not_found"), paperContribution: {status: "unavailable", detail: "No readable paper account is available.", report: null} as PaperContributionState};
  try {
    // Keep engine valuation, head, account, positions, fills and charges in one SQLite snapshot.
    db.exec("BEGIN");
    let portfolio = readPortfolioFromDatabase(db);
    const paperContribution = readPaperContribution(db, portfolio);
    if (portfolio.markMode === "engine_live" && ["invalid", "outdated", "unavailable"].includes(paperContribution.status)) portfolio = {...emptyPortfolio("invalid_account_evidence"), status: "invalid", markMode: "engine_live", markDisclaimer: paperContribution.detail};
    return {portfolio, paperContribution};
  } finally {db.close();}
}

function readPortfolioFromDatabase(db: DatabaseSync) {
    const live = readLivePortfolio(db);
    if (live) return live;
    if (!["paper_accounts", "paper_positions", "paper_ledger"].every((table) => hasTable(db, table))) {
      return emptyPortfolio("ledger_schema_incomplete");
    }
    const account = db.prepare(
      "SELECT starting_capital, cash_balance, updated_at FROM paper_accounts WHERE tenant_id=?",
    ).get(tenantId) as { starting_capital: string; cash_balance: string; updated_at: string } | undefined;
    if (!account) return emptyPortfolio("tenant_not_initialized");

    if (hasTable(db, "paper_replay_valuations")) {
      const snapshots = db.prepare(
        "SELECT timestamp, ledger_id, payload FROM paper_replay_valuations WHERE tenant_id=? ORDER BY timestamp",
      ).all(tenantId) as Array<{ timestamp: string; ledger_id: number; payload: string }>;
      const latest = snapshots[snapshots.length - 1];
      const ledgerHead = db.prepare(
        "SELECT COALESCE(MAX(id), 0) AS id FROM paper_ledger WHERE tenant_id=?",
      ).get(tenantId) as { id: number };
      // Fall back if a later trade has made the replay snapshot obsolete.
      if (latest && latest.ledger_id === ledgerHead.id) {
        const snapshot = JSON.parse(latest.payload) as {
          status: string; tenantId: string; markMode: string; markDisclaimer: string;
          cash: number; totalEquity: number; startingCapital: number;
          realizedPnl: number; unrealizedPnl: number; highWaterMark: number;
          drawdown: number; updatedAt: string;
          holdings: Array<{
            symbol: string; market: string; assetClass: string; quantity: number;
            averageEntry: number; markPrice: number; markSource: string;
            marketValue: number; unrealizedPnl: number;
          }>;
        };
        return {
          ...snapshot,
          equityCurve: snapshots.map((row) => ({
            timestamp: row.timestamp,
            equity: Number(JSON.parse(row.payload).totalEquity),
          })),
        };
      }
    }

    const entries = db.prepare(
      "SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id",
    ).all(tenantId) as LedgerRow[];
    const costs = hasTable(db, "paper_cost_ledger")
      ? (db.prepare("SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id").all(tenantId) as CostRow[])
      : [];
    const positions = db.prepare(
      "SELECT symbol, market, asset_class, quantity, average_price FROM paper_positions WHERE tenant_id=? ORDER BY symbol",
    ).all(tenantId) as Array<{
      symbol: string; market: string; asset_class: string; quantity: number; average_price: string;
    }>;

    const latestMark = new Map<string, number>();
    for (const entry of entries) latestMark.set(`${entry.market}:${entry.symbol}`, Number(entry.fill_price));
    const holdings = positions.map((position) => {
      const average = Number(position.average_price);
      const mark = latestMark.get(`${position.market}:${position.symbol}`) ?? average;
      const marketValue = mark * position.quantity;
      return {
        symbol: position.symbol,
        market: position.market,
        assetClass: position.asset_class,
        quantity: position.quantity,
        averageEntry: average,
        markPrice: mark,
        markSource: latestMark.has(`${position.market}:${position.symbol}`) ? "latest_ledger_fill" : "average_entry_fallback",
        marketValue,
        unrealizedPnl: (mark - average) * position.quantity,
      };
    });
    const realizedGross = calculateRealized(entries);
    const cashFees = costs.filter((row) => Boolean(row.cash_debit)).reduce((sum, row) => sum + Number(row.amount), 0);
    const realizedPnl = realizedGross - cashFees;
    const unrealizedPnl = holdings.reduce((sum, row) => sum + row.unrealizedPnl, 0);
    const cash = Number(account.cash_balance);
    const totalEquity = cash + holdings.reduce((sum, row) => sum + row.marketValue, 0);
    const equityCurve = buildEquityCurve(Number(account.starting_capital), entries, costs);
    // The engine persists its intra-run peak (paper_accounts.peak_equity) so the drawdown
    // breaker survives restarts; the tile shows that same peak, not just the fill-to-fill one.
    const persistedPeak = hasColumn(db, "paper_accounts", "peak_equity")
      ? Number((db.prepare("SELECT peak_equity FROM paper_accounts WHERE tenant_id=?").get(tenantId) as { peak_equity: string | null } | undefined)?.peak_equity ?? 0)
      : 0;
    const highWaterMark = Math.max(Number(account.starting_capital), persistedPeak, ...equityCurve.map((point) => point.equity));
    const drawdown = highWaterMark > 0 ? Math.max(0, (highWaterMark - totalEquity) / highWaterMark) : 0;
    return {
      status: "ok",
      tenantId,
      markMode: "ledger_marked",
      markDisclaimer: "Open positions use the latest known paper-ledger fill for that symbol, falling back to average entry. No live market feed is queried by this read-only UI.",
      cash,
      totalEquity,
      startingCapital: Number(account.starting_capital),
      realizedPnl,
      unrealizedPnl,
      highWaterMark,
      drawdown,
      holdings,
      equityCurve,
      updatedAt: account.updated_at,
    };
}

function calculateRealized(entries: LedgerRow[]): number {
  const state = new Map<string, PositionState>();
  let realized = 0;
  for (const entry of entries) {
    const key = `${entry.market}:${entry.asset_class}:${entry.symbol}`;
    const current = state.get(key) ?? { quantity: 0, average: 0, market: entry.market, assetClass: entry.asset_class };
    const price = Number(entry.fill_price);
    if (entry.side === "BUY") {
      const quantity = current.quantity + entry.quantity;
      current.average = quantity > 0 ? ((current.average * current.quantity) + price * entry.quantity) / quantity : 0;
      current.quantity = quantity;
    } else {
      realized += (price - current.average) * Math.min(entry.quantity, current.quantity);
      current.quantity = Math.max(0, current.quantity - entry.quantity);
      if (current.quantity === 0) current.average = 0;
    }
    state.set(key, current);
  }
  return realized;
}

function buildEquityCurve(startingCapital: number, entries: LedgerRow[], costs: CostRow[]): EquityPoint[] {
  const costByOrder = new Map<string, number>();
  for (const cost of costs) {
    if (!cost.cash_debit) continue;
    costByOrder.set(cost.order_id, (costByOrder.get(cost.order_id) ?? 0) + Number(cost.amount));
  }
  let cash = startingCapital;
  const quantity = new Map<string, number>();
  const marks = new Map<string, number>();
  const points: EquityPoint[] = [];
  for (const entry of entries) {
    const key = `${entry.market}:${entry.asset_class}:${entry.symbol}`;
    const price = Number(entry.fill_price);
    const notional = price * entry.quantity;
    cash += entry.side === "BUY" ? -notional : notional;
    cash -= costByOrder.get(entry.order_id) ?? 0;
    quantity.set(key, (quantity.get(key) ?? 0) + (entry.side === "BUY" ? entry.quantity : -entry.quantity));
    marks.set(key, price);
    let marketValue = 0;
    for (const [positionKey, qty] of quantity) marketValue += Math.max(0, qty) * (marks.get(positionKey) ?? 0);
    points.push({ timestamp: entry.created_at, equity: cash + marketValue });
  }
  return points;
}

function emptyPortfolio(reason: string) {
  return {
    status: "empty",
    reason,
    tenantId,
    markMode: "ledger_marked",
    markDisclaimer: "No readable initialized Pramana paper ledger is available.",
    cash: 0,
    totalEquity: 0,
    startingCapital: 0,
    realizedPnl: 0,
    unrealizedPnl: 0,
    highWaterMark: 0,
    drawdown: 0,
    holdings: [],
    equityCurve: [],
    updatedAt: null,
  };
}
