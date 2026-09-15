# AI holdout evidence contract

`quant_ai.validation.ai_holdout.evaluate_ai_holdout` evaluates a recorded AI
decision stream on an untouched, pre-declared holdout. It uses the position at
the prior observation to calculate close-to-close returns, charges entry, exit
and turnover costs, and compares the result with a cost-adjusted buy-and-hold
baseline. Invalid prices, non-binary positions, misaligned observations,
short holdouts and incomplete provenance fail closed.

Every report is `pramana.ai_holdout.v1` and records the model, model version,
strategy hash, data hash and decision cutoff. The report always sets
`promotion_approved` to `false`; an independent reviewer must assess the
report together with calibration/drift, execution-cost stress and forward
paper evidence before X03 can pass. This artifact is evidence plumbing, not a
profit forecast or a live-execution permission.

`quant_ai.validation.ai_quality.assess_calibration_drift` produces a paired
`pramana.ai_quality.v1` report from recorded probabilities and binary outcomes.
It reports Brier score, calibration error and first-versus-latest-window drift.
It rejects malformed or undersized samples and also never approves promotion.

Risk Lab attribution reads `PRAMANA_PORTFOLIO_RISK_METADATA_FILE` (or the
bounded inline override) and is wired into Compose through the read-only
`deploy/portfolio-risk-metadata.example.json` binding. The example is empty by
design: operators must replace it with a reviewed, timestamped source before
any sector/factor result can qualify as evidence.

The authenticated `GET /api/portfolio/attribution` route exports the same
no-store result with `Content-Disposition` when mappings are complete; an
unavailable result returns HTTP 422 and no attachment.

The reader requires an ISO-8601 timestamp with an explicit offset, rejects
unknown fields, caps the source at 500 symbols/1 MB, and withholds the whole
result when any held symbol is unmapped or has invalid market value.

The evaluator deliberately does not model intrabar queue priority, latency,
market impact, corporate actions or provider authenticity. Those are separate
qualification requirements in the pilot closure register.
