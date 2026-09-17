# Item 3: reviewed 50-name NSE watchlist expansion

Status: implementation in progress. Do not deploy this branch, change the running
five-name pilot, or treat a source download as acceptance.

Parent: owner-merged main bf3812088e4308eb5372203669d9e07cd8e74562.
The owner's AWS screenshot at 2026-09-17T19:09:01Z reported all three required
risk gates armed and data-ready, with 600 records / 119 aligned intervals and
five instruments covered. This is screenshot evidence, not an independently
retrieved raw host record. It permits starting Item 3, not widening production.

## Intended scope

- Keep one branch/PR: feat/widen-nse-watchlist. Owner merges and deploys.
- Retain the five existing instruments and risk-off ETF intent; use an explicitly
  selected 50-instrument configuration rather than replacing five-name defaults.
- Select diversified NSE cash equities from a current official NSE constituent
  snapshot. Index membership is a large-cap/liquidity selection proxy, not a
  claim about current execution spreads, individual liquidity or future return.
- Derive exact token/symbol mappings from the current NSE Kite instrument master
  using the existing normalizer, never hand-typed provider identifiers.
- Preserve pilot cap 50, health cap 64, financial risk limits, positions cap,
  money mode, ledger, existing credentials and model-budget settings.
- Record missing mappings, stale/ambiguous identities and insufficient sources
  as refusals. A single-sector selection must not be treated as diversified.
- Explain tenfold per-cadence workload and check the actual existing model-call
  and token budgets; no automatic budget increase or invented currency cost.
- Include paired control / deliberate-breakage / restoration verification.

## Public source references

NSE's own Nifty 50 page links the constituent CSV:
https://www.nseindia.com/static/products-services/indices-nifty50-index
https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv

Kite documents the exchange-specific instrument master here:
https://kite.trade/docs/connect/v3/market-quotes/
https://api.kite.trade/instruments/NSE

The temporary development source-evidence workflow uses no broker credentials,
requests only these public endpoints once each with TLS verification and bounded
responses, and archives the exact tracked source for independent local review.
A refused endpoint stays refused; no alternate credentials, rate-limit bypass,
paid model call, account mutation or trading daemon is used. Artifact timestamps
prove retrieval time only, not the underlying data's effective date or entitlement.

## Remaining acceptance

Actual selection, generator, deployment examples, mapping/runtime failure tests,
source qualification, cost model, full-suite results and guard-sensitivity evidence
remain required. Fresh runtime readiness for all 50 must be verified after owner
rollout; the earlier five-name success cannot be reused as 50-name acceptance.
