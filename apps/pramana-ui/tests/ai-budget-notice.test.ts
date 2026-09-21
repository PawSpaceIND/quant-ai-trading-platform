import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AiBudgetNotice } from "../components/decision-quality";
import { parseDecisionQuality } from "../lib/decision-quality-model";

const scope = {
  day: "2026-09-21", scope: "consensus", calls: 1, tokens: 100, reserved_tokens: 0,
  daily_call_limit: 2, daily_token_limit: 1000,
  remaining_calls: 1, remaining_tokens: 900, exhausted: false,
};
const shared = {
  calls: 2, tokens: 200, reserved_tokens: 0,
  remaining_calls: 0, remaining_tokens: 800, exhausted: true,
};
function render(budget: unknown) {
  const fixture = JSON.parse(fs.readFileSync(new URL("./fixtures/decision-quality.json", import.meta.url), "utf8"));
  fixture.ai_budget = budget;
  const parsed = parseDecisionQuality(JSON.stringify(fixture));
  assert.ok(parsed?.ai_budget);
  return { budget: parsed.ai_budget, html: renderToStaticMarkup(createElement(AiBudgetNotice, { budget: parsed.ai_budget })) };
}

test("shared exhaustion reaches the rendered warning despite consensus headroom", () => {
  const { budget, html } = render({ ...scope, aggregate: shared });
  assert.equal(budget.exhausted, false);
  assert.equal(budget.aggregate?.exhausted, true);
  assert.match(html, /AI budget exhausted in this report/);
  assert.match(html, /2 of 2 shared calls/);
  assert.match(html, /0 calls and 800 tokens remain for consensus/);
  assert.match(html, /consensus: 1 calls and 100 tokens used/);
  assert.match(html, /quality-budget warning/);
  assert.doesNotMatch(html, /every tick ends in PRESERVE_CAPITAL/);
});

test("reserved tokens consume shared headroom before usage is reconciled", () => {
  const { budget, html } = render({ ...scope, daily_call_limit: 10, aggregate: {
    ...shared, remaining_calls: 8, reserved_tokens: 800, remaining_tokens: 0,
  } });
  assert.equal(budget.aggregate?.reserved_tokens, 800);
  assert.match(html, /800 tokens reserved for pending or unreported usage/);
  assert.match(html, /AI budget exhausted/);
});

test("nonzero shared headroom does not promise the next reservation will fit", () => {
  const { html } = render({ ...scope, aggregate: {
    ...shared, calls: 1, remaining_calls: 1, remaining_tokens: 100, reserved_tokens: 700, exhausted: false,
  } });
  assert.match(html, /1 calls and 100 tokens remain/);
  assert.match(html, /headroom does not guarantee admission/);
  assert.doesNotMatch(html, /AI budget exhausted/);
});

test("old and malformed aggregate reports preserve scope but mark headroom unknown", () => {
  for (const aggregate of [undefined, null, {}, "healthy", { ...shared, calls: -1 }, { ...shared, calls: 1.5 }, { ...shared, reserved_tokens: "0" }]) {
    const { budget, html } = render({ ...scope, aggregate });
    assert.equal(budget.aggregate, null);
    assert.match(html, /incomplete shared usage/);
    assert.match(html, /Account-wide headroom is unknown/);
    assert.match(html, /quality-budget warning/);
  }
});

test("missing reservations are unknown, never silently zero", () => {
  const { html } = render({ ...scope, aggregate: { ...shared, exhausted: false, remaining_calls: 1, reserved_tokens: undefined } });
  assert.match(html, /Token reservations were not reported/);
  assert.match(html, /incomplete shared usage/);
});

test("a scope limit and zero headroom still warn even with an inconsistent false flag", () => {
  const { html } = render({ ...scope, remaining_calls: 0, aggregate: {
    ...shared, exhausted: false, remaining_calls: 1,
  } });
  assert.match(html, /AI budget exhausted/);
});
