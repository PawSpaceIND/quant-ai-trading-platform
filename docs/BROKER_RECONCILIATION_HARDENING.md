# Broker reconciliation hardening — PR #127

## Enforced scope

This is a read-only provider-evidence bridge into the local OMS, not a broker order
transport or permission to trade. It never changes the paper broker's cash/positions.
Legacy OMS intents only reconcile Indian cash equity/ETF observations from NSE/BSE;
derivatives require contract-aware decision-time identity and remain unsupported here.
The first accepted observation binds the broker account reference, order ID, instrument
ID, exchange and product durably. Subsequent changes are discrepancies, including after
restart. This observed binding does not prove the original intent carried a full contract.

## Current evidence, not stale reassurance

The default freshness policy is 300 seconds, configurable via
`max_capture_age_seconds`; nonpositive or noninteger windows refuse. The default clock is
current UTC. Archival tests explicitly pass a pinned `now`; they are not production
captures. Future, naive-clock and expired captures cannot advance the OMS. External
account comparisons also validate snapshot scope, INR currency, equity segment, source
broker, bounded capture interval and before/after stability. Hashes detect accidental
content changes; they are not provider signatures or proof of source authenticity.

## Economic and transactional integrity

Equal filled quantity is not enough: aggregate fill price and already imported individual
fill payloads must agree. Terminal orders observed again (and same-day bound orders) stay
in reconciliation. Unknown explicit bindings and two local claims on one broker order
refuse. Opaque callback fills with a positive unmatched broker delta remain ambiguous;
no trade lineage is guessed or duplicated.

The complete reconciliation pass uses one `BEGIN IMMEDIATE` write transaction. Nested
OMS mutations use savepoints so a later exception cannot commit earlier partial changes.
The event chain is checked against immutable intent, legal state transitions, broker
binding, fill history and aggregate projections before and after reconciliation.

Legacy `KITE:<exchange>:<trade ID>` fill identifiers are retained in this patch. They are
not a multi-account/day global identifier standard: a collision refuses atomically rather
than being renamed or guessed. Multi-account imported-fill namespacing remains an explicit
follow-up. Real account lifecycle and settlement evidence are still required for release.

## Verification on the macOS host

- Existing focused suite: 62 passing tests.
- Nine adversarial cases reproduced failures before the implementation changed.
- Twenty new adversarial cases plus the existing focused suite: 82 passing tests.
- Full branch: 1,430 passed; the existing 13 Bash-3/BSD deployment tests failed.
- Ruff across `src tests`: clean. No test skip, xfail or exposure/risk limit relaxation.
- Removing batch atomicity, capture freshness or price matching turns its regression red;
  the original implementation is restored after each deliberate sabotage.

No live-money order placement, risk-limit increase or source qualification is introduced.
