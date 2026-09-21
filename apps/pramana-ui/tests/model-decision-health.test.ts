import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ModelDecisionHealth } from "../components/decision-quality";
import { parseDecisionQuality } from "../lib/decision-quality-model";

function read(health: unknown) {
  const report = JSON.parse(fs.readFileSync(new URL("./fixtures/decision-quality.json", import.meta.url), "utf8"));
  report.counts.decisions = 5;
  report.inference_health = health;
  const parsed = parseDecisionQuality(JSON.stringify(report));
  assert.ok(parsed, "optional diagnostics must not hide the whole quality report");
  return { health: parsed.inference_health, html: renderToStaticMarkup(createElement(ModelDecisionHealth, { report: parsed.inference_health })) };
}
const sample = () => ({ decisions: 5, recorded: 4, not_recorded: 1,
  statuses: [{ status: "completed", decisions: 1 }, { status: "not_requested", decisions: 1 },
    { status: "budget_exhausted", decisions: 1 }, { status: "invalid_schema", decisions: 1 }],
  failures: [{ code: "budget_exhausted", decisions: 1 }, { code: "output_truncated", decisions: 1 }],
});

test("show complete, no-request, budget and schema states without claiming an API error rate", () => {
  const { health, html } = read(sample());
  assert.equal(health?.recorded, 4);
  for (const text of ["Model response completed", "No model requested", "Budget blocked",
    "Response failed validation", "output truncated", "1 have no recorded diagnostic",
    "not API request counts or provider error rates"]) assert.ok(html.includes(text), text);
});

test("legacy and malformed diagnostics remain unknown instead of fabricating zero failures", () => {
  for (const broken of [undefined, null, {}, { ...sample(), recorded: 5 }, { ...sample(), decisions: 6 },
    { ...sample(), not_recorded: -1 }, { ...sample(), recorded: 4.5 },
    { ...sample(), statuses: [{ status: "PRIVATE", decisions: 4 }] },
    { ...sample(), failures: [{ code: "provider_auth", decisions: 7 }] }]) {
    const { health, html } = read(broken);
    assert.equal(health, null);
    assert.match(html, /No valid model diagnostic breakdown/);
    assert.doesNotMatch(html, /PRIVATE/);
  }
});

test("unknown provider strings never enter the rendered failure table", () => {
  const { html } = read({ ...sample(), failures: [{ code: "PRIVATE PROVIDER RESPONSE", decisions: 1 }] });
  assert.match(html, /unclassified failure/);
  assert.doesNotMatch(html, /PRIVATE PROVIDER/);
});

test("an entirely legacy window explicitly reports missing diagnostic coverage", () => {
  const { html } = read({ decisions: 5, recorded: 0, not_recorded: 5, statuses: [], failures: [] });
  assert.match(html, /0 of 5 decisions carry diagnostics/);
  assert.match(html, /Decisions without diagnostics remain unknown/);
});
