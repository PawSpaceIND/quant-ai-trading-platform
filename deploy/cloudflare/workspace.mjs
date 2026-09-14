// Adapt persisted read-only snapshots to the shared UI. A copied heartbeat is
// never presented as a currently executing protection loop.
export function workspaceSnapshot(snapshots, sourceAt) {
  const supplied = snapshots["/api/workspace"];
  const portfolio = {
    ...(supplied?.portfolio || snapshots["/api/portfolio/mtm"]),
    status: "snapshot",
    allMarksFresh: false,
  };
  portfolio.holdings = (portfolio.holdings || []).map((h) => ({
    ...h,
    fresh: false,
    markSource: "published_snapshot",
  }));
  portfolio.markDisclaimer = `Hosted snapshot from ${sourceAt}. Current engine/ledger state is not verified here. ${portfolio.markDisclaimer || ""}`;
  const market = { ...(supplied?.market || snapshots["/api/market"]) };
  market.rows = (market.rows || []).map((r) => ({
    ...r,
    lastTrade: r.lastTrade?.startsWith("1970") ? undefined : r.lastTrade,
    exchangeTimestamp: r.exchangeTimestamp?.startsWith("1970")
      ? undefined
      : r.exchangeTimestamp,
  }));
  const age = Date.now() - Date.parse(market.fetchedAt || "");
  market.collectorStale = !Number.isFinite(age) || age > 120000 || age < -5000;
  return {
    portfolio,
    market,
    intelligence:
      supplied?.intelligence || snapshots["/api/intelligence/swarm"],
    runtime: {
      ...(supplied?.runtime || {}),
      mode: "paper",
      status: "snapshot",
    },
    performance: supplied?.performance || {
      status: "insufficient_data",
      days: 0,
      daily: [],
      sharpe: null,
      sortino: null,
      netReturn: null,
      source: "not_published",
    },
    research: supplied?.research || null,
    audit: [],
    tenantId: "india-paper",
    haltRequested: false,
    copilotConfigured: false,
    generatedAt: sourceAt,
    liveEnabled: false,
    checks: [
      {
        id: "hosted",
        title: "Hosted observation only",
        pass: false,
        detail:
          "Use the authenticated engine workspace for live protection status and release review. Snapshot synchronization is not execution readiness.",
      },
    ],
  };
}
