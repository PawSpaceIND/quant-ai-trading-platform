# Specialist scope and ETF reference evidence

An Indian ETF is not an Indian operating company. `IndianEquitiesAgent` and
`USEquitiesAgent` now abstain on every non-equity asset, even when a provider
returns company ratios. `CommodityYieldAgent` currently models macro effects on
individual stocks, so it also abstains on ETFs, indices, metals and derivatives.
This prevents equity-specific valuation and risk-off assumptions from becoming
votes about silver or gold funds. It does not introduce a trained metals model.

The pipeline adds `etf-value-reference`, a read-only reference reader. It always
has zero confidence, zero expected return/risk and a neutral stance. Its RISK
domain excludes it from directional coverage. It cannot supply the missing third
directional specialist, authorize a trade, or veto a position. The minimum
specialist coverage, confidence floors and all execution controls stay unchanged.
An ETF may therefore remain on hold when only news and technical evidence apply.

## Participation and historical decisions

New decision proofs persist a finite participation status, reason code and role
alongside the original rationale. The dashboard distinguishes not applicable,
missing data, unavailable sources, abstention, informed neutral views, vetoes and
reference observations. Raw specialist rationale and provider exceptions are not
copied into these public labels. Known provider failure codes receive bounded
English descriptions. Old proofs without these fields say “Reason not recorded”;
the UI does not infer a cause from a zero score or rewrite past decisions.
The displayed directional summary excludes risk/liquidity controls.

## Optional indicative fund value input

`PRAMANA_ETF_INAV_FILE` points to an operator-supplied JSON observation file. It is
unset by default. In Docker, `/data/etf-inav.json` must exist **inside the existing
pramana-data volume**, not merely beside the host checkout. Compose passes the
setting to the engine; this change does not install a collector or populate the
file. A separate authorized fund-data source/collector is required. Write complete
snapshots atomically and retain original source evidence independently.

Synthetic format example — these are test values, not market data:

```json
{
  "schema": "pramana.etf_inav.v1",
  "observations": [{
    "symbol": "SILVERBEES",
    "market": "INDIA",
    "exchange": "NSE",
    "currency": "INR",
    "kind": "indicative_nav",
    "value": "100.00",
    "observed_at": "2026-09-21T08:00:00+00:00",
    "received_at": "2026-09-21T08:00:01+00:00",
    "source": "synthetic-test-source"
  }]
}
```

Requirements:

- Exact symbol, market, exchange and currency identity; one matching observation.
- Indicative NAV per unit, in the quote currency. Closing NAV is not accepted.
- Positive finite decimal value supplied as a string, bounded to 0.000001–1 billion.
- Timezone-aware source and receipt times, source ≤ receipt ≤ decision time.
- Source observation and matching market quote each no more than 60 seconds old.
- File ≤1 MB, at most 500 rows, no duplicate JSON keys, no unexpected fields in the
  document or selected row. Source is a bounded ASCII identifier.

The result is `(quote / indicative NAV - 1) × 10,000` basis points. It records both
values/timestamps, receipt time, source identifier and observation hash in the
Atlas evidence context and decision provenance. A source label and hash establish
what was supplied; they do not independently authenticate the publisher. Missing,
invalid, stale or unmatched inputs produce explicit status codes instead of a
premium/discount estimate.

This comparison is context, not executable arbitrage or a directional forecast.
It does not include creation/redemption access, spreads, depth, fees, tracking
error, market impact or the path of the underlying commodity. Those require
separate data and strategy validation. Options, futures, currency, debt and other
market expertise are not completed by this change.

## Validation

The regression suite covers non-stock scope despite favorable company ratios,
identity and timestamp mismatches, malformed/duplicate/oversized inputs, fresh
reference arithmetic, sync/async pipeline persistence and unchanged coverage
holds. Existing stock paper-fill tests ensure the new reference does not disable
otherwise qualified stock trades. UI tests cover old proofs, finite public labels,
provider-error privacy and exclusion of control vetoes from directional scoring.
