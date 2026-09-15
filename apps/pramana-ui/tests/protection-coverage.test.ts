import {test} from "node:test";
import assert from "node:assert/strict";
import type {Runtime} from "../lib/pilot";
import {protectionCoverageCheck} from "../lib/protection-coverage";

const now = Date.parse("2026-09-15T06:00:00Z");
const portfolio = {status: "ok", tenantId: "pilot", markMode: "engine_live", ledgerId: 5, holdings: [{}]};
const runtime: Runtime = {status: "running", mode: "paper", halted: false, protectionCoverage: {
  schema: "pramana.protection_coverage.v1", tenantId: "pilot", status: "complete", ledgerId: 5,
  checkedAt: new Date(now).toISOString(), positionCount: 1, coveredCount: 1, missingStopCount: 0,
  invalidPositionCount: 0, issueCount: 0, issues: [], scope: "stored_paper_levels_only",
}};
test("current ledger-bound protection is distinct from operator halt and empty positions are valid", () => {
  assert.equal(protectionCoverageCheck(runtime, portfolio, "pilot", now).pass, true);
  assert.equal(protectionCoverageCheck({...runtime, halted: true}, portfolio, "pilot", now).pass, true);
  assert.equal(protectionCoverageCheck({...runtime, protectionCoverage: {...runtime.protectionCoverage!,
    positionCount: 0, coveredCount: 0}}, {...portfolio, holdings: []}, "pilot", now).pass, true);
});
test("missing protection explains affected holdings and never passes", () => {
  const check = protectionCoverageCheck({...runtime, protectionCoverage: {...runtime.protectionCoverage!,
    status: "incomplete", coveredCount: 0, missingStopCount: 1, issueCount: 1,
    issues: [{key: "INDIA:EQUITY:INFY", code: "missing_stop"}]}}, portfolio, "pilot", now);
  assert.equal(check.pass, false);
  assert.match(check.detail, /INFY: missing_stop/);
  assert.equal(protectionCoverageCheck({...runtime, protectionCoverage: null}, portfolio, "pilot", now).pass, false);
});
test("stale, foreign, malformed, inconsistent and later-fill coverage fails closed", () => {
  for (const changes of [
    {checkedAt: "invalid"}, {checkedAt: new Date(now - 10001).toISOString()},
    {checkedAt: new Date(now + 5001).toISOString()}, {tenantId: "other"}, {schema: "unknown"},
    {ledgerId: 4}, {ledgerId: 6}, {ledgerId: NaN}, {status: "invalid"},
    {issueCount: 1}, {issues: [{key: "INFY", code: "invalid_stop"}]},
    {positionCount: 0}, {coveredCount: 0}, {missingStopCount: 1}, {invalidPositionCount: 1},
  ]) {
    const state = {...runtime, protectionCoverage: {...runtime.protectionCoverage!, ...changes}};
    assert.equal(protectionCoverageCheck(state, portfolio, "pilot", now).pass, false, JSON.stringify(changes));
  }
  for (const changes of [{status: "stale"}, {status: "invalid"}, {markMode: "ledger_marked"},
    {ledgerId: undefined}, {ledgerId: 6}, {tenantId: "other"}, {holdings: []}]) {
    assert.equal(protectionCoverageCheck(runtime, {...portfolio, ...changes}, "pilot", now).pass, false);
  }
  assert.equal(protectionCoverageCheck({...runtime, status: "stale"}, portfolio, "pilot", now).pass, false);
});
