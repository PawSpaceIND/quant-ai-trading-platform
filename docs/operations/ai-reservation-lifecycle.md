# Request-bound AI token reservations

New Anthropic consensus/headline and optional Astra calls use `ai_budget_requests`
inside the existing token ledger. Tickets carry original UTC day, scope (caller path),
allowance, creation/dispatch times, linked dollar ticket ID and settled token usage.
Admission and its ticket insertion are one SQLite transaction. Dispatch is marked
before provider I/O. Dollar denial compensates the admitted token ticket exactly once;
shared denial similarly compensates the optional shadow sample ticket. Valid usage
settles once against the original day, before validating output content. Both input and
output counts must be complete nonnegative integers; any supplied cache counts must
also be valid. Partial, malformed and all-zero usage retains the whole allowance.
Absent optional Anthropic cache fields are omitted; OpenAI input already includes cache.

Historical aggregates have no request provenance. They remain legacy unknown and are
neither backfilled nor released. Unknown usage, timeouts, network failures and dispatched
requests remain reserved. Settlement failure rolls back rather than opening headroom.
The original aggregate-only API is retained for compatibility; provider clients use the
new request-bound API.

The token, shadow and dollar SQLite files are not crash-atomic together. A crash after
one admission, after dollar admission, or during partial shadow dispatch can retain
allowance without a provider call. Do not automatically cancel old admitted rows: the
linked dollar ticket might not yet have been recorded. A dispatch marker describes
possible provider I/O, not proof the provider received it. Manual reconciliation requires
positive evidence and separate authorization. No recovery sweep or reservation reset
is introduced.

The new table is created when a budget store starts after a future approved deployment;
this change performs no production migration. Dollar request schema/pricing, caps,
trading policy and chat retries are unchanged. There is no automatic migration/release
of today's reservations. Full dollar lifecycle provenance, provider response IDs and
read-only reconciliation tooling remain follow-up work.
