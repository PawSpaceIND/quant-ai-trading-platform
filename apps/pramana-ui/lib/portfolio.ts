import { CostRow, hasTable, LedgerRow, openLedger, tenantId } from "@/lib/db";

type PositionState = { quantity: number; average: number; market: string; assetClass: string };

export type EquityPoint = { timestamp: string; equity: number };

export function readPortfolio() {
  const db = openLedger();
  if (!db) return emptyPortfolio("ledger_not_found");
  try {
    if (!["paper_accounts", "paper_positions", "paper_ledger"].every((table) => hasTable(db, table))) {
      return emptyPortfolio("ledger_schema_incomplete");
    }
    const account = db.prepare(
      "SELECT starting_capital, cash_balance, updated_at FROM paper_accounts WHERE tenant_id=?",
    ).get(tenantId) as { starting_capital: string; cash_balance: string; updated_at: string } | undefined;
    if (!account) return emptyPortfolio("tenant_not_initialized");

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
    const highWaterMark = Math.max(Number(account.starting_capital), ...equityCurve.map((point) => point.equity));
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
  } finally {
    db.close();
  }
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
  return points.length ? points : [{ timestamp: new Date(0).toISOString(), equity: startingCapital }];
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
