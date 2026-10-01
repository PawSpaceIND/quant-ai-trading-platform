# Recorded research input coverage

The read-only session funnel now includes `research_coverage`, scoped to the same
IST half-open session and tenant as recorded journal evaluations. Its three admission
states sum to recorded evaluations: candidate quorum met, candidate quorum below,
and unknown admission. Missing journal tables return null, not a zero-coverage verdict.

Only v1 admission evidence with valid distinct agent identities, roles, nonnegative
integer ages/budgets, actual boolean predicates and consistent age comparison is counted.
Legacy, malformed or contradictory evidence is unknown with no partially inferred input
counts. Empty known agent lists mean below quorum. Gate agents never contribute candidates.
Reported aggregate counters are recomputed from validated entries rather than trusting
stored totals. Candidates remain distinct from votes cast or entry admission: an earlier
stale or risk gate may hold even with enough candidates.

Two separate age denominators are retained: evaluations containing an over-age input,
and over-age agent entries. Several stale agents in one evaluation count once in the
first and separately in the second. These are recorded Atlas evidence ages, not current
feed freshness or independent provider authenticity. Unknown observations cannot be
reclassified fresh. Existing gate/inference outcome counts remain separate dimensions;
this report does not assign causal blame or reconstruct missing scheduler cycles.

No migration, journal rewrite, provider call, forecast training, risk policy or runtime
configuration change is included. Forecast scoring remains the existing report field.
Selector cache coverage and non-journal cadence completeness still require separate
source evidence; this summary cannot prove them from admission snapshots.
