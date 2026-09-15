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

The evaluator deliberately does not model intrabar queue priority, latency,
market impact, corporate actions or provider authenticity. Those are separate
qualification requirements in the pilot closure register.
