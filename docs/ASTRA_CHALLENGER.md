# Astra challenger for Atlas

Atlas can compare Anthropic and `gpt-6-astra` on the exact same evidence prompt.
The primary Anthropic response still determines the proposed paper trade. Astra cannot
replace a failed primary, vote in the consensus, bypass risk controls, or issue orders.
This integration adds no live-money route and does not automatically promote a model.

## Activation and verification

The feature is **off by default**. A key by itself does not turn it on.

1. Configure `OPENAI_API_KEY` in the pilot's secret environment. Do not commit it or
   paste it into chat. Keep the existing Anthropic key and enabled shared AI budget.
2. After installing the reviewed build, run one isolated connectivity test in the
   daemon environment: `python -m quant_ai.llm.astra_probe --confirm-paid-call`.
   This sends synthetic missing-evidence context, counts against the existing budget,
   and has no broker connection. It reports only validation status, model and usage.
3. Set `PRAMANA_ASTRA_SHADOW_ENABLED=true` for the paper daemon when ready to collect
   comparisons, then restart through the normal deployment procedure. Docker Compose
   forwards both new settings only to the ghost service. Disabling the flag restores
   the unchanged primary-only path.
4. Inspect `provenance.inference.model_comparison` in the existing durable XAI JSON
   proofs. A comparison contains a unique pair ID, common prompt hash, each model's
   raw validated consensus, returned model identity, timing, token usage and failure
   status. Invalid primary responses also retain the comparison in their held proof.
   Confirm matching prompt hashes and that ledger orders belong only to the primary.

The SDK is not needed: the adapter uses the existing httpx dependency and the fixed
OpenAI Responses endpoint. Requests use strict structured output, `store=false`,
medium reasoning and an 8,192-token ceiling including reasoning. No retry or model
substitution is performed. Incomplete/refused/invalid output fails closed; unknown
spend retains its reservation. API errors are reduced to finite codes, not raw text.

## Budget and timing

Astra has a persistent allowance of at most **20 requests and 300,000 reserved/used
tokens per UTC day**, inside the existing account-wide call and token cap. Its extra
ledger lives beside the shared budget with suffix `.astra-shadow`; restarts do not
reset the allowance. Usage includes reasoning; cached input is not double-counted.
These are token limits, not a fixed rupee or dollar cost guarantee.

Both calls run concurrently after the existing Atlas pre-inference gates. Astra has
a 30-second total timeout. A slower challenger can delay the primary result until
that timeout; market freshness and pre-submit controls must still pass. Measure this
latency during paper observation before expanding the sample. The allowance takes
the first eligible calls of a day: this small connectivity/behavior pilot is **not a
representative all-session or all-market performance experiment**.

## What constitutes improvement

The recorded `confidence` is a model's declared conviction, not a calibrated
probability. Do not apply Brier scoring to it as if it were a forecast probability.
The existing consensus contract does not specify a forecast horizon; comparing its
raw expected returns is not yet a matched-horizon forecasting evaluation.

This PR measures agreement, completion, rejection reasons, latency and token usage.
Subsequent forecast evaluation must predeclare a horizon and target, record predictions
before outcomes, join only subsequently observed prices, include transaction costs,
and compare against simple baselines and Anthropic on identical eligible cases.
Only a validated, representative result can justify changing execution authority.
The separate numerical-model holdout evaluator remains a distinct research process.

Local tests use mocked provider responses, including a full cadence that fills exactly
one paper order while Astra disagrees. They do not establish real API access, a
deployment, successful production trading or a profitable edge.

API references: [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra),
[structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
