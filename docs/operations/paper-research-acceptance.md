# Offline paper research acceptance

`operations.paper_research_acceptance` joins the existing cached-history readers,
prior-session shortlist, two-cycle selector and chronological after-cost evaluator.
It adds no runtime switch, fetch/warmup, subscriptions, database writes, orders or
model promotion. It is a library for controlled qualification, not an operator
activation interface. Live runtime behavior is unchanged.

`cached_research_coverage` accepts the existing authorized catalog (maximum50)
and only the exact audited Yahoo/Kite daily-history reader types. It copies at
most64 bars per eligible tradable stock from the current-day memory cache.
An explicit exchange calendar defines the latest21 closed prior sessions;
the supplied previous session must match that calendar. Future/target-day and
non-session rows cannot replace missing history. Instrument identities must match.
Reports distinguish absent current-day cache, insufficient prior sessions,
missing latest prior session, unsupported reader and malformed/error evidence.
An observation-only row or ETF is counted in the catalog but not the eligible
stock denominator. No fresh cache is created by this check. Provider authenticity,
corporate-action adjustment and entitlement remain separate qualifications.

`qualify_paper_research` uses that bounded immutable copy in the existing shortlist
engine, then runs the actual selector on two declared same-session cycles.
It reports supported catalog, research universe, shortlist, fixed comparator,
per-symbol entry refusals, held retention and unchanged protection universe.
Incomplete coverage deterministically exercises fixed fallback; complete history
does not imply sector/liquidity eligibility or fresh subscription delivery.
The supplied ticks must already be validated observations; the source test itself
cannot prove the host received them. Existing later-cycle/fresh-tick checks apply.
The runner never executes a daemon, broker order or risk decision. Its output
always denies trading/activation authorization and host-readiness verification.

`evaluate_frozen_paper_research` requires a declared plan freeze before every
holdout and delegates fitting/scoring to the existing expanding-window evaluator.
All half-open, non-overlapping holdouts are retained, late training labels are
purged by the existing evaluator, and pooled metrics count each held-out row once.
It reports whether both pooled after-cost Brier and log-loss improvements exceed
zero against each existing constant-half/training-prevalence baseline. These are
descriptive fixed criteria, not statistically validated superiority or promotion.
The freeze is caller-declared, not authenticated prospective registration. This
forecast comparison is distinct from a fixed-versus-dynamic portfolio comparison;
it does not turn opportunity labels into realized trading profit.

The finite release acceptance remains: qualify real authorized catalog/cache and
bound-paper identities privately; retain all held-position protection; measure
recorded decision/order/fill conservation; register a frozen prospective fixed-list
comparison before collecting results; reconcile costs, prices and outcomes
independently. Existing risk, capital, quorum, spend and protection gates stay in
force. No count target forces names or trades: a10-stock eligible catalog cannot
produce15 distinct eligible choices without separately reviewed admission.

Synthetic tests demonstrate software boundaries, not current AWS data coverage,
market edge, probability calibration, profitability or live-money readiness.
