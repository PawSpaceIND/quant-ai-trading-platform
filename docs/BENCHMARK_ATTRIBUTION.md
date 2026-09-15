# Single-period benchmark attribution

`quant_ai.analytics.benchmark_attribution.benchmark_attribution(document)` implements arithmetic Brinson-Fachler allocation, selection and interaction for supplied INR long-only beginning weights and same-period total returns. Include cash as an explicit sector when applicable. Every sector needs both portfolio and benchmark returns; missing returns are never replaced by zero. Weights must sum exactly to one and supplied aggregate returns must reconcile exactly to the weighted sector returns. Use decimal strings, at most 18 decimal places.

Allocation is `(portfolioWeight - benchmarkWeight) * (sectorBenchmarkReturn - totalBenchmarkReturn)`. Selection is `benchmarkWeight * (sectorPortfolioReturn - sectorBenchmarkReturn)`. Interaction is the product of active weight and active sector return. The sum must equal portfolio return minus benchmark return.

The input schema is `pramana.benchmark_attribution_input.v1`, with `currency: INR`, `basis: beginning_weights_same_period_total_returns`, `portfolioReturn`, `benchmarkReturn`, and `sectors`. Each sector supplies `name`, `portfolioWeight`, `benchmarkWeight`, `portfolioReturn`, and `benchmarkReturn`. All returns are fractions, not percentage points. The test fixture is a complete example.

The independent numerical regression uses Table 8 of [Achmea Investment Management's Shapley Attribution research](https://www.achmeainvestmentmanagement.nl/-/media/files/institutioneel/nieuws/aim-shapley-attributie-research-paper.pdf): allocation 0.50%, selection -1.30%, interaction 0.05%, active return -0.75%. See also [CFA Institute's performance attribution review](https://rpc.cfainstitute.org/sites/default/files/-/media/documents/book/rf-lit-review/2019/rflr-performance-attribution.pdf).

This is a calculation component, not complete Bloomberg PORT parity. It does not generate qualified beginning weights, sector classifications or total-return histories. Source receipts, period/currency alignment, corporate-action/income accounting, fee treatment, multi-period linking, publication, exports and dashboard/Atlas wiring remain open. Do not feed current holdings into historical attribution. The existing exposure panel remains exposure-only until these inputs and integrations are implemented and verified.

Generate a private report with:

```sh
python scripts/benchmark_attribution.py --input /data/reviews/attribution-input.json --output /data/reviews/attribution-report.json
```

The command limits input to 1 MB, includes the supplied inputs and their exact byte SHA-256, writes with mode 0600, and refuses to overwrite an existing output. Invalid inputs exit 2 without creating a report. The source hash identifies the reviewed file; it does not authenticate its contents. Keep source and report in a private review directory. This command does not automatically publish to the dashboard or qualify a pilot gate.

Inputs also require `portfolioId`, `benchmarkId`, `periodStart` and `periodEnd`. Dates use exact YYYY-MM-DD format and must increase. They label the beginning and ending valuation dates; the caller must qualify that every sector return and beginning weight uses this same interval. These fields are retained in the report, which explicitly sets `sourceQualified: false`. Identifiers are descriptive labels, not authenticated account bindings. The numerical regression uses synthetic labels/dates around the published arithmetic example.
