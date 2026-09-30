# Source freshness and Atlas admission

This source-only change fails closed on unavailable or future observations and expired
streamed dependencies. It does not approve trading or change capital, risk, AI quotas,
provider configuration, quorum, execution or protection controls.

## Reproduced inconsistency and retained policy

At main `c4ccaf42599192dd9de96784d0aa41c8691ab783`, the India equity specialist gets full
source weight with fundamentals aged 7,200 seconds and news aged 20 seconds. Its
aggregate age is 7,200 seconds, so Atlas holds with `stale_specialist_evidence` under
the existing 3,600-second COUNTRY budget. This remains a hold. Full source weight
means current for the source's publication cadence; it is not Atlas admission.
The two clocks now remain visible in specialist rationale even at full weight.

A broader policy change requires review. Options are to retain the conservative
one-hour domain budget, or to introduce typed per-source admission with separately
reviewed slow-source budgets while retaining strict news/price limits. Replacing the
aggregate age with the newest timestamp, raising the global one-hour guard or making
missing evidence age zero for admission would conceal source age and is unsuitable.
No such admission change is in this PR. ETF's two-voter/three-quorum gap is separate.

## Contract

- PRICE: configured TTL 60 seconds. NEWS: configured TTL 1,800 seconds. Both must be
  FRESH to give a dependent specialist any weight. Expired data gives zero confidence
  and a neutral stance; Atlas excludes that unusable voter under its existing rules.
- FUNDAMENTAL: configured TTL 86,400 seconds. MACRO: configured TTL 604,800 seconds.
  Existing slow-source confidence penalties and Atlas domain caps remain intact.
- TTL equality is current. A fractional second beyond the TTL is stale; integer ages
  round upward conservatively. No future observation is current, even within one
  microsecond: INVALID, unknown age, zero multiplier. MISSING likewise has unknown
  age and zero multiplier. Naive timestamps remain rejected.
- Each specialist rationale records its declared source names, states, ages and TTLs
  for full and reduced weights. The legacy aggregate age field still contains the
  oldest known dependency age; it cannot represent unknown age and must be interpreted
  with the source diagnostic and confidence, never alone as evidence of freshness.

Both deterministic and LLM pipeline paths call the same dependency/multiplier helper.
India equity depends on news and fundamentals; US equity also depends on macro;
geopolitical depends on news; technical/liquidity/risk desks depend on price;
commodity depends on macro. The shared fast-input rule covers all pipeline asset
classes (equity, ETF, commodity, forex, futures and options where supported).
It adds no new asset support or voters. Execution quote/protection checks are unchanged.

## Evidence and limits

The initial corrected regression set on unchanged main: 19 failed, 3 passed. Failures
covered future timestamps across all four categories, fractional TTL expiry, expired
fast dependencies retaining weight, invalid sources retaining an India stance, and
missing/full-weight provenance. Fresh inputs and existing missing-data abstention
already passed. After the fix, the same 22 cases pass, with added asset-class and
missing-time coverage. Tests use fixed clocks, constructed source snapshots and
rule-agent/Atlas calls only; no live data or provider calls.

These tests characterize supplied snapshot timestamps, not provider timestamp lineage.
The pipeline still uses its existing latest-news aggregation and declared dependency
mapping; whether older contributing headlines or optional India tape metrics need
separate clocks is follow-up review. No historical simulation, production path frequency,
live effectiveness or readiness is established. Focused validation: 161 tests passed across source freshness, intelligence pipeline,
Atlas rule/MAS/LLM overlay, ETF specialist scope and mocked Anthropic swarm tests.
Focused Ruff and `git diff --check` passed. Full builds/suite were not run locally.
