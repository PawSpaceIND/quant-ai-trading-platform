import { reviewedGate } from "@/lib/review";
import { readResearch } from "@/lib/research";
import fs from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { readPortfolio } from "@/lib/portfolio";
import { readMarket } from "@/lib/market";
import { latestSwarmIntelligence } from "@/lib/proofs";
import { readRuntime, performance } from "@/lib/pilot";
import { consoleDb } from "@/lib/console-db";
import { ledgerPath, tenantId } from "@/lib/db";
export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const market = await readMarket();
    const portfolio = readPortfolio();
    const runtime = readRuntime();
    const perf = performance();
    const db = consoleDb();
    let audit;
    try {
      audit = db
        .prepare(
          "SELECT action,detail,at FROM audit WHERE tenant=? ORDER BY id DESC LIMIT 30",
        )
        .all(tenantId);
    } finally {
      db.close();
    }
    const haltedFile =
      process.env.PRAMANA_HALT_FILE ||
      path.join(path.dirname(ledgerPath()), "PRAMANA_HALT");
    const strategyReview = reviewedGate("strategy"),
      recoveryReview = reviewedGate("recovery");
    const completedTrades = runtime.tradeEvidence?.status === "ok" ? runtime.tradeEvidence.summary?.completedTrades : undefined;
    const reconciliationAge = Date.now() - Date.parse(runtime.reconciliation?.checkedAt || "");
    const checks = [
      {
        id: "reconciliation",
        title: "Paper account reconciliation",
        pass: runtime.status === "running" && runtime.reconciliation?.status === "matched"
          && reconciliationAge >= -5000 && reconciliationAge <= 120000,
        detail: runtime.reconciliation
          ? `${runtime.reconciliation.status}; ledger ${runtime.reconciliation.ledgerId}; ${runtime.reconciliation.issueCount} issues; checked ${runtime.reconciliation.checkedAt}. Internal paper consistency only. Checked at startup and before entries; later fills require a new check.`
          : "No paper account reconciliation recorded. External broker reconciliation is separate.",
      },
      {
        id: "engine",
        title: "Protection heartbeat",
        pass: runtime.status === "running",
        detail: runtime.updatedAt || "No engine heartbeat recorded",
      },
      {
        id: "scope",
        title: "INR cash pilot scope",
        pass:
          !!runtime.watchlist?.length &&
          runtime.watchlist.every(
            (i) =>
              i.market === "INDIA" &&
              i.currency === "INR" &&
              i.exchange === "NSE" &&
              ["EQUITY", "ETF"].includes(i.assetClass),
          ),
        detail: "One currency · NSE cash equities and ETFs",
      },
      {
        id: "quotes",
        title: "Market collector",
        pass: market.status === "ok" && !market.collectorStale,
        detail: market.fetchedAt || "No market collector available",
      },
      {
        id: "marks",
        title: "Current portfolio valuation",
        pass: portfolio.markMode === "engine_live" && portfolio.status === "ok",
        detail: portfolio.markDisclaimer,
      },
      {
        id: "ticks",
        title: "Trading feed coverage",
        pass:
          !!runtime.watchlist?.length &&
          runtime.watchlist.every((i) => i.fresh),
        detail:
          "Every engine watchlist instrument needs a tick within 120 seconds",
      },
      {
        id: "evidence",
        title: "Forward observation",
        pass: perf.days >= 30 && strategyReview.pass && Number.isInteger(completedTrades) && (completedTrades ?? 0) >= 100,
        detail: `${perf.days} observed days; ${completedTrades ?? "unavailable"} completed paper trade episodes (minimum 100). ${strategyReview.detail} Counts alone are not approval.`,
      },
      {
        id: "recovery",
        title: "Deployment recovery sign-off",
        pass: recoveryReview.pass,
        detail: recoveryReview.detail,
      },
    ];
    return NextResponse.json(
      {
        portfolio,
        market,
        runtime,
        research: readResearch(),
        performance: perf,
        intelligence: latestSwarmIntelligence(),
        checks,
        audit,
        tenantId,
        haltRequested: fs.existsSync(/* turbopackIgnore: true */ haltedFile),
        copilotConfigured: !!process.env.ANTHROPIC_API_KEY,
        release: "private-paper-pilot",
        liveEnabled: false,
        generatedAt: new Date().toISOString(),
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      {
        error:
          "Workspace data is unavailable. Check ledger and console storage.",
      },
      { status: 503 },
    );
  }
}
