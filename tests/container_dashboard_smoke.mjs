// Runs inside the isolated dashboard container against its real npm-start server.
import assert from "node:assert/strict";
import fs from "node:fs";

const origin = process.env.SMOKE_ORIGIN || "http://localhost:3000";
assert.ok(["localhost", "127.0.0.1"].includes(new URL(origin).hostname));
const phase = process.env.SMOKE_PHASE;
const protectionMissing = ["missing-protection", "protection-restarted"].includes(phase);
const invalidLedger = ["invalid-ledger", "ledger-restarted"].includes(phase);
const faultHalted = protectionMissing || phase === "protection-restored" || invalidLedger || ["ledger-restored", "stream-rejections"].includes(phase);
const anonymous = await fetch(`${origin}/api/workspace`);
assert.equal(anonymous.status, 401);
assert.equal((await fetch(`${origin}/api/research/attribution`)).status, 401);
assert.equal((await fetch(`${origin}/api/broker/external-account`)).status, 401);
// Persist one test-only signed session across phases/restarts without relaxing the
// product's login rate limit. Anonymous rejection is still checked on every phase.
const sessionFile = process.env.SMOKE_SESSION_FILE;
let cookie = sessionFile && fs.existsSync(sessionFile) ? fs.readFileSync(sessionFile, "utf8") : null;
if (!cookie) {
  const login = await fetch(`${origin}/api/session`, {
    method: "POST", headers: {origin, "Content-Type": "application/json"},
    body: JSON.stringify({secret: process.env.PRAMANA_DASHBOARD_SECRET}),
  });
  assert.equal(login.status, 200);
  cookie = login.headers.get("set-cookie")?.split(";")[0];
  if (sessionFile) fs.writeFileSync(sessionFile, cookie, {mode: 0o600});
}
assert.ok(cookie?.startsWith("pramana_session="));
async function request(route, method = "GET", body, requestOrigin = origin) {
  return fetch(origin + route, {method, headers: {cookie, origin: requestOrigin, "Content-Type": "application/json"},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})});
}
const response = await request("/api/workspace");
assert.equal(response.status, 200);
assert.match(response.headers.get("cache-control"), /no-store/);
const workspace = await response.json();
assert.equal(workspace.tenantId, process.env.PRAMANA_TENANT_ID);
assert.equal(workspace.liveEnabled, false);
assert.equal(workspace.copilotConfigured, false);
assert.equal(workspace.runtime.mode, "paper");
assert.equal(workspace.externalAccount.status,"stale");
assert.equal(workspace.externalAccount.report.status,"consistent");
assert.equal(workspace.externalAccount.report.positions[0].quantity,"2");
const externalDownload=await request("/api/broker/external-account");
assert.equal(externalDownload.status,200);
assert.match(externalDownload.headers.get("cache-control"),/no-store/);
assert.deepEqual(await externalDownload.json(),workspace.externalAccount.report);

assert.equal(workspace.benchmarkAttribution.status, "published");
assert.equal(workspace.benchmarkAttribution.report.portfolioId, "example-portfolio");
assert.equal(workspace.benchmarkAttribution.report.sourceQualified, false);
assert.equal(Number(workspace.benchmarkAttribution.report.activeReturn), -0.0075);
assert.equal(workspace.benchmarkAttribution.report.sectors.length, 4);
assert.equal(Object.hasOwn(workspace.benchmarkAttribution.report, "inputPayload"), false);
assert.equal(Object.hasOwn(workspace.benchmarkAttribution.report, "input"), false);
const attributionDownload=await request("/api/research/attribution");
assert.equal(attributionDownload.status,200);
assert.match(attributionDownload.headers.get("cache-control"),/no-store/);
assert.match(attributionDownload.headers.get("content-disposition"),/attachment/);
assert.deepEqual((await attributionDownload.json()).report,workspace.benchmarkAttribution.report);

const feedCheck = workspace.checks.find(c => c.id === "ticks");
assert.ok(feedCheck);
if (phase === "stale") {
  assert.equal(feedCheck.pass, false);
} else {
  assert.equal(feedCheck.pass, true);
  assert.equal(workspace.runtime.watchlist.length, 1);
  assert.equal(workspace.runtime.watchlist[0].fresh, true);
  assert.equal(workspace.runtime.watchlist[0].freshnessReason, "fresh");
  assert.ok(workspace.runtime.watchlist[0].tickTimestamp);
}
if (phase === "stream-rejections") {
  const integrity = workspace.runtime.marketDataIntegrity;
  assert.equal(integrity.schema, "pramana.tick_integrity.v1");
  assert.ok(integrity.accepted > 0);
  for (const code of ["out_of_order_tick", "invalid_tick_values", "duplicate_tick", "future_tick"]) assert.ok(integrity.rejected[code] > 0);
  assert.equal(integrity.lastRejection.reason, "future_tick");
  assert.equal(workspace.portfolio.holdings[0].markPrice, 100);
}
assert.equal(workspace.checks.find(c => c.id === "recovery").pass, false);
assert.equal(workspace.checks.find(c => c.id === "evidence").pass, false);
if (invalidLedger) {
  assert.equal(workspace.portfolio.status, "invalid");
  assert.equal(workspace.portfolio.holdings.length, 0);
  assert.match(workspace.portfolio.markDisclaimer, /invalid_position_average/);
  assert.equal(workspace.paperContribution.status, "invalid");
  assert.equal(workspace.runtime.valuation.status, "unavailable");
  assert.equal(workspace.checks.find(c => c.id === "marks").pass, false);
} else {
  assert.equal(workspace.portfolio.holdings.length, 1);
  assert.equal(workspace.portfolio.holdings[0].symbol, "INFY");
  assert.equal(workspace.portfolio.holdings[0].quantity, 2);
  assert.equal(workspace.portfolio.startingCapital, 123456);
}
assert.equal(workspace.runtime.limits.maxPositions, 3);
assert.equal(workspace.runtime.protectionCoverage.status, invalidLedger ? "invalid" : protectionMissing ? "incomplete" : "complete");
assert.equal(workspace.runtime.protectionCoverage.tenantId, workspace.tenantId);
assert.equal(workspace.runtime.protectionCoverage.positionCount, 1);
if (!invalidLedger) assert.equal(workspace.runtime.protectionCoverage.ledgerId, workspace.portfolio.ledgerId);
assert.equal(workspace.checks.find(c => c.id === "protection_coverage").pass, phase !== "stale" && !protectionMissing && !invalidLedger);
if (protectionMissing) assert.equal(workspace.runtime.protectionCoverage.issues[0].code, "missing_stop");
if (faultHalted) assert.equal(workspace.runtime.haltReason, "paper_position_protection_incomplete");
const researchMissing = phase === "missing-research";
if (researchMissing) {
  for (const name of ["researchLab", "researchPortfolio", "companyEvents"]) assert.equal(workspace[name].status, "invalid");
  assert.equal(workspace.researchLab.report, null);
  assert.equal(workspace.researchPortfolio.report, null);
  assert.deepEqual(workspace.companyEvents.events, []);
} else {
assert.equal(workspace.researchLab.status, "published");
assert.equal(workspace.researchLab.report.registered_cases, 1);
assert.equal(workspace.researchLab.report.candidates.active.missing_decisions, 1);
assert.equal(workspace.researchLab.report.automatic_promotion, false);
assert.equal(workspace.researchPortfolio.status, "published");
assert.equal(Number(workspace.researchPortfolio.report.books.active.cash_inr), 720);
assert.equal(Number(workspace.researchPortfolio.report.books.active.current_equity_inr), 1050);
assert.equal(workspace.researchPortfolio.report.books.active.holdings[0].quantity, 3);
assert.equal(workspace.companyEvents.status, "available");
assert.equal(workspace.companyEvents.events[0].title, "Infosys Limited");
assert.equal(workspace.companyEvents.events[0].mapping.symbol, "NSE:INFY");
assert.equal(workspace.companyEvents.events[0].captureKind, "imported");
}
for (const [route, matches] of [
  ["/api/research/comparison", r => r.experiment === "deployment-fixture" && r.tenant_id === workspace.tenantId],
  ["/api/research/portfolio", r => r.name === "Synthetic deployment replay" && Number(r.books.active.current_equity_inr) === 1050],
  ["/api/company-events?download=1", r => r.events[0].description === "Synthetic deployment disclosure"],
]) {
  assert.equal((await fetch(origin + route)).status, 401);
  const downloaded = await request(route);
  assert.equal(downloaded.status, researchMissing ? 503 : 200);
  assert.match(downloaded.headers.get("cache-control"), /no-store/);
  if (!researchMissing) {
    assert.match(downloaded.headers.get("content-disposition"), /attachment/);
    assert.ok(matches(await downloaded.json()));
  }
}
if (phase === "stale") {
  assert.equal(workspace.runtime.status, "stale");
  assert.equal(workspace.checks.find(c => c.id === "engine").pass, false);
  assert.equal(workspace.checks.find(c => c.id === "entry_controls").pass, false);
} else {
  assert.equal(workspace.runtime.status, "running");
  assert.equal(workspace.runtime.halted, phase === "halted" || faultHalted);
  assert.equal(workspace.checks.find(c => c.id === "entry_controls").pass, phase !== "halted" && !faultHalted);
  assert.equal(workspace.portfolio.status, invalidLedger ? "invalid" : "ok");
  if (!invalidLedger) assert.equal(workspace.portfolio.holdings[0].markPrice, 100);
  assert.equal(workspace.market.rows[0].symbol, "INFY");
}
if (phase === "initial") {
  assert.equal((await request("/api/watchlist", "PUT", {symbols:["INFY"]}, "https://wrong.invalid")).status, 403);
  assert.equal((await request("/api/watchlist", "PUT", {symbols:["INFY"]})).status, 200);
  assert.equal((await request("/api/control", "POST", {action:"resume", reason:"unsupported"})).status, 400);
}
assert.deepEqual(await (await request("/api/watchlist")).json(), {symbols:["INFY"]});
if (phase === "initial") {
  const halt = await request("/api/control", "POST", {action:"halt", reason:"Isolated container smoke drill"});
  assert.equal(halt.status, 200);
  assert.equal((await halt.json()).status, "requested");
}
console.log(JSON.stringify({phase, status:"pass", authenticated:true, liveEnabled:false,
  ...(phase === "stream-rejections" ? {streamIntegrity:workspace.runtime.marketDataIntegrity} : {}),
  valuationStatus:workspace.runtime.valuation?.status, portfolioStatus:workspace.portfolio.status,
  protectionCoverage:workspace.runtime.protectionCoverage.status,
  protectionCheck:workspace.checks.find(c => c.id === "protection_coverage").pass,
  runtime:workspace.runtime.status, halted:workspace.runtime.halted, savedWatchlist:["INFY"],
  accountQuantity:invalidLedger ? null : 2, operatorAcceptance:false, customDirectives:{startingCapital:123456,maxPositions:3},
  research:{comparison:workspace.researchLab.status, portfolio:workspace.researchPortfolio.status,
    companyEvents:workspace.companyEvents.status, authenticatedExportStatus:researchMissing ? 503 : 200, endpointsChecked:3}}));
