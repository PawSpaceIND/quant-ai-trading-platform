# AI learning and knowledge system

The trading AI is allowed to be **broad in research and narrow in authority**. The objective is not to make an LLM more aggressive. It is to give candidate models the best permitted evidence, train/evaluate them causally, measure whether their probabilities and after-cost outcomes are actually useful, and keep deterministic risk/execution gates outside the model.

## Knowledge plane

`quant_ai.learning.knowledge` separates three access planes:

- **Research** can inspect a wide evidence set, including explicitly unverified-rights material, but that material stays labelled and cannot silently become a training or decision input.
- **Training** requires a point-in-time source and verified/internal rights.
- **Decision** requires the same plus freshness checks and any operator-declared mandatory evidence categories.

Supported knowledge categories include live market and order-book state, fundamentals, macro, news, corporate events, derivatives, broker observations, portfolio/risk/execution state, decision history, research and operator-approved lessons. Every item pins `observed_at`, `available_at`, source, content hash and reference, preventing later information from masquerading as knowledge available at the decision time.

## Training plane

A training dataset manifest freezes the data cutoff, source snapshot, feature schema, label schema, cost policy and corporate-action adjustment policy. A training run then pins model family, dataset, code/configuration hashes, seed and the produced artifact hash.

The training wrapper is deliberately model-agnostic: statistical models, neural models or an externally trained model can all produce an artifact, but no artifact gains execution authority from being trained.

## Probability and edge

A trading probability must correspond to a predeclared outcome such as `P(after-cost net return > 0 at horizon H)`. Generic LLM confidence is not treated as that probability.

A paper candidate must satisfy the existing strategy-promotion evidence *and* demonstrate probability skill against a paired baseline. Current candidate assessment requires positive after-cost expectancy, drawdown within the configured policy, enough resolved probability observations and Brier score better than the paired baseline. These are configurable engineering gates, not a promise of future profit.

## Champion/challenger

Candidate lifecycle is:

`RESEARCH -> SHADOW -> PAPER_CANDIDATE -> PAPER_APPROVED`

Promotion events are hash-chained. `PAPER_APPROVED` requires a named reviewer. There is intentionally **no live-approved stage** in this module. Real-money authorization remains a separate release with broker/regulatory/operational gates.

A paper-approved model can be demoted back to shadow. This is important for calibration or regime drift: the platform should reduce trust when evidence deteriorates rather than teach the model to take more risk to recover losses.

## Learning feedback

The existing specialist-attribution engine can learn bounded weights from realised outcomes. The super-platform closure contract additionally requires sample floors, bounded weights and forward review. A later enhancement should score agents with exposure-normalized outcomes and proper probability scoring rather than attributing raw P&L alone.
