import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Calibration, Headline, HoldCausesPanel, PostMortems, QualityTables, VerdictBanner } from "../components/decision-quality";
import { DASH, RATE_MINIMUM, parseDecisionQuality, parsePostMortem, rateShown, thinNote, thinRate, verdictFor } from "../lib/decision-quality-model";

const fixture = () => JSON.parse(fs.readFileSync(new URL("./fixtures/decision-quality.json", import.meta.url), "utf8"));
const postMortem = () => JSON.parse(fs.readFileSync(new URL("./fixtures/post-mortem.json", import.meta.url), "utf8"));
const holds = () => ({ holds: 20, hard_hold: 3, silent: 9, deadlock: 5, conviction_floor: 2, roster_unrecorded: 1 });
const promotion = () => ({
  schema: "pramana.promotion_report.v1", verdict: "missing_inputs", promotion_authorized: false,
  minimum_resolved: 200, resolved_forecast_count: 41, basis: "weighted_lean_times_confidence.v1",
  missing_inputs: ["drawdown_or_policy_unavailable"],
});
const parse = (patch: (r: ReturnType<typeof fixture>) => void) => {
  const r = fixture();
  patch(r);
  return parseDecisionQuality(JSON.stringify(r));
};

test("a rate is shown only over its own sample, and never when the sample is unknown", () => {
  assert.equal(RATE_MINIMUM, 20);
  assert.equal(thinRate(1, 1, 20), DASH, "one position is not a 100% hit rate");
  assert.equal(thinRate(0, 1, 20), DASH, "nor a 0% one");
  assert.equal(thinRate(0.55, 19, 20), DASH);
  assert.equal(thinRate(0.55, 20, 20), "55.00%");
  assert.equal(thinRate(0.55, null, 20), DASH);
  assert.equal(thinRate(0.55, undefined, 20), DASH);
  assert.equal(rateShown(20, 20), true);
  assert.equal(rateShown(Number.NaN, 20), false);
  assert.equal(thinNote(1, 20), "1 of 20 needed for a rate");
  assert.equal(thinNote(null, 20), "sample size not reported");
});

test("the hold split and the promotion gate parse, and a block that describes other rows is dropped", () => {
  const parsed = parse((r) => { r.holds = holds(); r.promotion = promotion(); });
  assert.deepEqual(parsed?.holds, holds());
  assert.equal(parsed?.promotion?.verdict, "missing_inputs");
  assert.deepEqual(parsed?.promotion?.missing_inputs, ["drawdown_or_policy_unavailable"]);

  const legacy = parse(() => {});
  assert.ok(legacy, "a report written before either block still reads");
  assert.equal(legacy.holds, null);
  assert.equal(legacy.promotion, null);

  for (const broken of [
    { ...holds(), silent: 10 },           // parts no longer sum to the total
    { ...holds(), holds: 9999, silent: 9988 }, // more holds than the report has decisions
    { ...holds(), deadlock: -1, silent: 15 },
    { ...holds(), deadlock: 2.5, silent: 11.5 },
  ]) assert.equal(parse((r) => { r.holds = broken; })?.holds, null, JSON.stringify(broken));

  for (const broken of [
    { ...promotion(), promotion_authorized: true },            // a flag that disagrees with its verdict
    { ...promotion(), verdict: "pass" },                       // and the other way round
    { ...promotion(), schema: "pramana.promotion_report.v2" }, // numbers under another schema
    { ...promotion(), minimum_resolved: -200 },
  ]) {
    const report = parse((r) => { r.promotion = broken; });
    assert.ok(report, "a malformed gate never blanks the report");
    assert.equal(report.promotion, null, JSON.stringify(broken));
    assert.equal(verdictFor(report).state, "insufficient_sample");
  }
});

test("each grouped hit rate carries its sample; an older report's rows read with it unknown", () => {
  const parsed = parse((r) => {
    r.by_regime = [{ regime: "ranging", decisions: 90, filled: 1, evaluated: 1, hit_rate: 1, net_pnl: -24.61 }];
    r.by_hour_ist = [{ hour: 10, decisions: 40, hit_rate: 0.5, net_pnl: 0 }];
  });
  assert.equal(parsed?.by_regime[0].evaluated, 1);
  assert.equal(parsed?.by_hour_ist[0].evaluated, null);
  assert.equal(parse((r) => { r.by_regime[0].evaluated = -1; }), null);
});

test("the headline shows counts, not rates, over one closed trade and a handful of calls", () => {
  const thin = parse((r) => {
    r.directional = { horizon_minutes: 60, evaluated: 1, hit_rate: 1, mean_forward_return: 0.004 };
    r.trades = { ...r.trades, closed: 1, win_rate: 0, expectancy: -24.61, profit_factor: 0, average_win: null, average_loss: 24.61, net_pnl: -24.61 };
    r.calibration.brier_score = 0.04;
  });
  assert.ok(thin);
  const html = renderToStaticMarkup(createElement(Headline, { report: thin }));
  const tile = (markup: string, label: string) => markup.match(new RegExp(`<span>${label}</span><strong[^>]*>([^<]*)</strong>`))?.[1];
  for (const label of ["Hit rate 60m", "Expectancy", "Profit factor", "Brier score"]) assert.equal(tile(html, label), DASH, label);
  for (const text of ["1 evaluated · 1 of 20 needed for a rate", "1 closed · 1 of 20 needed for a rate"]) assert.ok(html.includes(text), text);
  assert.equal(tile(html, "Net P&amp;L"), "-24.61", "the realised P&L is a fact and stays");

  const ample = renderToStaticMarkup(createElement(Headline, { report: parse(() => {})! }));
  assert.equal(tile(ample, "Hit rate 60m"), "55.88%", "34 evaluated clears the report's minimum of 20");
});

test("the banner says insufficient sample, never edge, while the gate refuses", () => {
  const report = parse((r) => { r.promotion = promotion(); })!;
  const html = renderToStaticMarkup(createElement(VerdictBanner, { verdict: verdictFor(report) }));
  assert.ok(html.includes("Insufficient sample · 34 of 200 directional decisions evaluated"));
  assert.ok(html.includes("Promotion: missing inputs"));
  assert.ok(!html.includes("Edge candidate"));
  assert.ok(!html.includes("pill green"), "no check is painted green while the rule is not read");
});

test("holds are shown by cause, and a report without the split says so", () => {
  const html = renderToStaticMarkup(createElement(HoldCausesPanel, { holds: holds(), decisions: 61 }));
  for (const text of ["20 of 61 decisions held", "Deadlock", "Silent", "Conviction floor", "Hard hold", "Roster not recorded", "Never probed"]) {
    assert.ok(html.includes(text), text);
  }
  assert.match(html, /Deadlock<\/strong><\/td><td class="numeric">5</);
  const none = renderToStaticMarkup(createElement(HoldCausesPanel, { holds: null, decisions: 61 }));
  assert.match(none, /does not split its holds/);
});

test("a session post-mortem carries the sample behind its hit rate", () => {
  const withCount = postMortem();
  withCount.summary.evaluated_60m = 3;
  withCount.summary.by_regime = [{ regime: "ranging", decisions: 12, filled: 0, evaluated: 3, hit_rate: 0.3333, net_pnl: 0 }];
  const parsed = parsePostMortem(JSON.stringify(withCount));
  assert.equal(parsed?.summary.evaluated_60m, 3);
  assert.equal(parsed?.summary.by_regime[0].evaluated, 3);
  const legacy = parsePostMortem(JSON.stringify(postMortem()));
  assert.ok(legacy);
  assert.equal(legacy.summary.evaluated_60m, null);
});

test("completed-trade evidence shows no win rate over one trade", async () => {
  const { TradeEvidencePanel } = await import("../components/pilot-workspace");
  const data = (completedTrades: number, wins: number, losses: number) => ({
    runtime: {
      tradeEvidence: {
        status: "ok", currency: "INR", ledgerId: 1, fillCount: 2 * completedTrades, generatedAt: "2026-09-24T08:00:00Z", sourceSha256: "a".repeat(64),
        summary: { completedTrades, openEpisodes: 0, wins, losses, breakeven: 0, netPnl: "-24.61", expectancy: "-24.61", winRate: String(wins / completedTrades), profitFactor: "0", profitFactorState: "defined", closedCashFees: "12.00", openCashFees: "0" },
      },
      strategyEvidence: null,
    },
    strategyObservation: null,
  });
  const render = (d: ReturnType<typeof data>) => renderToStaticMarkup(createElement(TradeEvidencePanel, { data: d as never, onAsk: () => {} }));
  const tile = (markup: string, label: string) => markup.match(new RegExp(`<span>${label}</span><strong[^>]*>([^<]*)</strong>`))?.[1];
  const one = render(data(1, 0, 1));
  for (const label of ["Win rate", "Average P&amp;L per trade", "Profit factor"]) assert.equal(tile(one, label), DASH, label);
  assert.ok(one.includes("0 wins · 1 losses · 0 flat · 1 of 20 needed for a rate"));
  assert.ok(one.includes("-24.61"), "net closed-trade P&L is a fact and stays");
  const many = render(data(25, 15, 10));
  assert.equal(tile(many, "Win rate"), "60.00%");
});

test("breakdown rows, calibration bins and post-mortems withhold a rate over a thin sample", () => {
  const report = parse((r) => {
    r.by_regime = [
      { regime: "ranging", decisions: 90, filled: 1, evaluated: 1, hit_rate: 1, net_pnl: -24.61 },
      { regime: "trending_up", decisions: 60, filled: 4, evaluated: 25, hit_rate: 0.6, net_pnl: 310 },
      { regime: "legacy", decisions: 30, filled: 2, hit_rate: 0.7, net_pnl: 12 },
    ];
    r.by_agent = [{ agent_id: "geopolitical-analyst", evaluated: 3, directional_accuracy: 1 }];
  })!;
  const tables = renderToStaticMarkup(createElement(QualityTables, { report }));
  const regimeRows = [...tables.matchAll(/<tr><td>(ranging|trending up|legacy)<\/td>((?:<td[^>]*>.*?<\/td>)+)<\/tr>/g)]
    .map((m) => [m[1], [...m[2].matchAll(/<td[^>]*>(.*?)<\/td>/g)].map((c) => c[1])]);
  assert.deepEqual(regimeRows.map(([name, cells]) => [name, (cells as string[])[2], (cells as string[])[3]]), [
    ["ranging", "1", DASH], ["trending up", "25", "60.00%"], ["legacy", DASH, DASH],
  ]);
  assert.match(tables, /geopolitical-analyst<\/td><td class="numeric">3<\/td><td class="numeric">—</);

  const bins = [
    { lower: 0.5, upper: 0.6, decisions: 3, hit_rate: 1, mean_confidence: 0.55 },
    { lower: 0.6, upper: 0.7, decisions: 25, hit_rate: 0.6, mean_confidence: 0.65 },
  ];
  const strip = renderToStaticMarkup(createElement(Calibration, { bins, brier: 0.2, horizon: 60, minimum: 20 }));
  assert.equal((strip.match(/<span class="hit"/g) ?? []).length, 1, "only the bin with 25 decisions draws a hit bar");
  assert.ok(strip.includes("55.00% claimed · — hit") && strip.includes("65.00% claimed · 60.00% hit"));
  assert.ok(strip.includes("Brier 0.20"), "28 decisions across the strip clear the minimum");
  const sparse = renderToStaticMarkup(createElement(Calibration, { bins: bins.slice(0, 1), brier: 0.01, horizon: 60, minimum: 20 }));
  assert.ok(sparse.includes("Brier —"));

  const session = (evaluated: number | undefined, hit: number) => {
    const pm = postMortem();
    if (evaluated === undefined) delete pm.summary.evaluated_60m; else pm.summary.evaluated_60m = evaluated;
    pm.summary.hit_rate_60m = hit;
    return parsePostMortem(JSON.stringify(pm))!;
  };
  const html = (item: ReturnType<typeof session>) => renderToStaticMarkup(createElement(PostMortems, { items: [item] }));
  assert.match(html(session(1, 1)), /hit rate 60m<\/dt><dd>— \(1 evaluated\)</);
  assert.match(html(session(undefined, 0.5)), /hit rate 60m<\/dt><dd>— \(sample not recorded\)</);
  assert.match(html(session(24, 0.5)), /hit rate 60m<\/dt><dd>50\.00%</);
});
