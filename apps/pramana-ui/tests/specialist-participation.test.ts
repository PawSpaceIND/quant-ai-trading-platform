import assert from "node:assert/strict";
import test from "node:test";
import {specialistParticipation} from "../lib/specialist-participation";

test("zero scores distinguish applicability, missing data and unrecorded history", () => {
  const out = specialistParticipation({confidence:"0", participation:"not_applicable", reason_code:"non_equity_instrument", role:"directional"});
  const missing = specialistParticipation({confidence:"0", participation:"missing_data", reason_code:"india_fundamentals_insufficient", role:"directional"});
  const old = specialistParticipation({confidence:"0"});
  assert.equal(out.label,"Not applicable");
  assert.equal(missing.label,"Missing data");
  assert.equal(old.label,"Reason not recorded");
});
test("reference evidence is not represented as a directional specialist", () => {
  const value = specialistParticipation({participation:"observed",reason_code:"etf_reference_observed",role:"reference"});
  assert.equal(value.label,"Reference only");
  assert.equal(value.role,"reference");
  assert.match(value.reason,/not a buy signal/);
});
test("unknown, contradictory and raw private reasons cannot become public labels", () => {
  for (const row of [
    {participation:"ready",reason_code:"etf_reference_stale"},
    {participation:"ready",reason_code:"private provider payload"},
    {participation:"ready",reason_code:"toString"},
  ]) {
    const value = specialistParticipation({...row, rationale_json:"private prompt",role:"invented"});
    assert.equal(value.status,"unrecorded");
    assert.equal(value.role,"unrecorded");
    assert(!JSON.stringify(value).includes("private"));
  }
});
