import {readRunComparison} from "@/lib/run-comparison";
import {tradingFeedCheck} from "@/lib/freshness";
import {readBrokerObservation} from "@/lib/broker-observation";
import { currentStrategyEvidence, strategyObservationDays } from "@/lib/strategy-evidence";
import { reviewedGate, verifiedRuntimeManifest } from "@/lib/review";
import { readResearch } from "@/lib/research";
import { readResearchLab } from "@/lib/research-lab";
import { readPortfolioResearch } from "@/lib/research-portfolio";
import {readCompanyEvents} from "@/lib/company-events";
import fs from "node:fs";
import path from "node:path";
import { NextResponse } from "next/server";
import { readPortfolioSnapshot } from "@/lib/portfolio";
import { readMarket } from "@/lib/market";
import {historicalRisk} from "@/lib/historical-risk";
import { latestSwarmIntelligence } from "@/lib/proofs";
import { readRuntime, performance } from "@/lib/pilot";
import {protectionCoverageCheck} from "@/lib/protection-coverage";
import { consoleDb } from "@/lib/console-db";
import { ledgerPath, tenantId } from "@/lib/db";
export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const market = await readMarket();
    const {riskHistory: riskHistoryInput, ...displayMarket} = market;
    const {portfolio, paperContribution, benchmarkPerformance} = readPortfolioSnapshot(riskHistoryInput);
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
    const haltRequested = fs.existsSync(/* turbopackIgnore: true */ haltedFile);
    const strategyReview = reviewedGate("strategy", runtime),
      recoveryReview = reviewedGate("recovery");
    const strategySha = runtime.strategyManifest?.sha256;
    const strategyEvidence = runtime.strategyEvidence;
    const strategyObservation = strategyObservationDays(strategySha, strategyEvidence?.incompatibleSessionDates, strategyEvidence?.coverageStartedAt);
    const completedTrades = strategyEvidence?.status === "ok" ? strategyEvidence.summary?.completedTrades : undefined;
    const reconciliationAge = Date.now() - Date.parse(runtime.reconciliation?.checkedAt || "");
    const checks = [
      {
        id: "entry_controls",
        title: "Entry controls",
        pass: runtime.status === "running" && runtime.halted === false && !haltRequested,
        detail: runtime.halted ? `Entries halted: ${runtime.haltReason || "risk halt"}. Protective exits continue while the engine is running.`
          : haltRequested ? "Operator halt requested; awaiting engine acknowledgement."
          : runtime.status === "running" && runtime.halted === false ? "No active entry halt. Other qualification checks still apply."
          : "Entry-control state is unverified.",
      },
      {
        id: "strategy_manifest",
        title: "Running strategy configuration",
        ...verifiedRuntimeManifest(runtime),
      },
      {
        id: "strategy_evidence",
        title: "Strategy evidence coverage",
        pass: runtime.status === "running" && !!strategySha && currentStrategyEvidence(runtime,strategySha),
        detail: strategyEvidence ? `${strategyEvidence.unresolvedEpisodes} unresolved episodes; ${strategyEvidence.foreignOpenEpisodes} foreign or unproven open episodes. Coverage must be complete for the active configuration; account totals retain unproven trades.` : "No current configuration-linked episode report.",
      },
      {
        id: "reconciliation",
        title: "Paper account reconciliation",
        pass: runtime.status === "running" && runtime.reconciliation?.status === "matched"
          && reconciliationAge >= -5000 && reconciliationAge <= 120000,
        detail: runtime.reconciliation
          ? `${runtime.reconciliation.status}; ledger ${runtime.reconciliation.ledgerId}; ${runtime.reconciliation.issueCount} issues; checked ${runtime.reconciliation.checkedAt}. Internal paper consistency only. Checked at startup and before entries; later fills require a new check.`
          : "No paper account reconciliation recorded. External broker reconciliation is separate.",
      },
      protectionCoverageCheck(runtime, portfolio, tenantId),
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
      tradingFeedCheck(runtime),
      {
        id: "evidence",
        title: "Forward observation",
        pass: strategyObservation.days >= 30 && strategyReview.pass && !!strategySha && currentStrategyEvidence(runtime,strategySha) && Number.isInteger(completedTrades) && (completedTrades ?? 0) >= 100,
        detail: `${strategyObservation.days} configuration-qualified days; ${completedTrades ?? "unavailable"} strategy-linked completed trades (minimum 100). ${strategyReview.detail} Counts alone are not approval.`,
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
        paperContribution,
        benchmarkPerformance,
        brokerObservation: readBrokerObservation(),
        historicalRisk: historicalRisk(portfolio, riskHistoryInput),
        market: displayMarket,
        runtime,
        research: readResearch(),
        researchLab: readResearchLab(),
        researchPortfolio: readPortfolioResearch(),
        runComparison: readRunComparison(),
        companyEvents: readCompanyEvents(),
        performance: perf,
        strategyObservation,
        intelligence: latestSwarmIntelligence(),
        checks,
        audit,
        tenantId,
        haltRequested,
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
