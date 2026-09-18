# Dashboard review and confirmation of recorded bookkeeping recovery

Base: PR #152, `e2ed5ec4676acc8aea581598fa29f99d14d292d7`.
This is an opt-in operator interface over the existing authenticated recovery API.
It is not a trading interface, daemon launcher, credential issuer or recovery-policy
change. The existing read-only Paper OMS panel remains read-only and unchanged.

## Actual route and explicit configuration

Activity now includes a separate "Review recorded bookkeeping" panel. The founder
session authenticates the dashboard. The Node server then uses separately selected
read/apply possession keys to access the existing Python API. Those secrets are
never accepted from the browser or placed in browser bundles, storage or responses.

The server selects all four values; none has a deployed default:

- PRAMANA_OPERATOR_API_ORIGIN: an exact HTTPS origin, or HTTP on literal loopback
  127.0.0.1 / ::1. Paths, credentials, query fragments and plain remote HTTP refuse.
- PRAMANA_OPERATOR_TENANT: the intended recovery account, displayed during review.
- PRAMANA_OPERATOR_READ_KEY_FILE: existing absolute path to the read credential.
- PRAMANA_OPERATOR_APPLY_KEY_FILE: optional existing absolute path to the apply key.

Files must be regular, unaliased, owner-only and owned by the server process user.
Reads are bounded and use no-follow opening with inode/permission checks. Missing
apply configuration leaves inspection read-only. The Python service independently
enforces the actual key's permissions, expiry and revocation; a configured file
is not proof of permission. There is no credential import or setup action here.
Use the existing secure deployment secret-provisioning process, not a public key
field or committed environment file. The new environment example is commented out.

The backend now adds its authenticated tenant to programme previews and request
responses. This is an additive observation field, not a changed authorization rule.
The gateway verifies that account, programme/request ID and context match before
returning bounded fields. It checks preview/context before POST and detects changed
selected origin, tenant or apply credential before dispatch. Backend exact-context
checks still run. Neither proxy nor browser may override account, file, URL or action.

This is deliberate delegation for a trusted founder workspace, not independent
human identity or per-user role delegation. The displayed recovery account must be
reviewed explicitly; the browser tests use separate disposable dashboard/ledger and
recovery fixtures, and do not claim they are one real account. Production tenant
selection and secure gateway/credential provisioning still require operator review.

## User workflow and uncertainty

Enter a saved execution programme ID and inspect it. Read the displayed recovery
account, programme/slice state and exact saved context. The confirmation checkbox
must be selected before "Confirm bookkeeping reconciliation" is enabled.

Before submission, the browser saves only the generated request ID, selected tenant,
programme ID and context checksum in session storage. No credential or model data
is stored. Failed or malformed local tracking prevents a replacement submission.
Double clicks are suppressed. POST is never automatically retried by this feature.

A failed, truncated, timed-out or malformed POST response is not evidence that
bookkeeping did not commit. The request remains "Outcome not confirmed", including
after reload. "Check saved outcome" performs GET only. A missing response/404 does
not clear uncertainty. An outcome for another saved tenant/programme/context is
not accepted as the tracked request's result. A recorded RETURNED result is shown
as history, not as current account health or permission to trade. Another reviewed
request requires an explicit reset and new inspection/confirmation, not a retry loop.

The backend's existing durable audit, requested/outcome states and source idempotency
remain authoritative. The operation can reconcile recorded tenant protective-exit
bookkeeping, as stated in the panel. It cannot place orders, reopen positions, clear
halts, release risk capacity or resolve an absent broker receipt by assuming failure.

## Gateway and observation limits

The new /api/institutional-recovery route requires the existing dashboard session.
POST also checks the exact request origin, even when invoked without shared proxy
middleware. Bodies are bounded to 2 KiB and must contain only the programme ID,
request ID, exact context checksum and fixed bookkeeping-only confirmation.

The fixed upstream uses separate API keys, no redirects, no-store requests and a
bounded timeout/16 KiB response. Only validated observation/result fields return;
provider text, SQL, filesystem details, key values and raw upstream errors do not.
Responses, including authorization failures, use no-store. HTTP error or transport
failure after dispatch is treated conservatively as an unconfirmed outcome.

This is not a distributed transaction between the browser, gateway and Python service.
A concurrent backend change must still be handled by existing context/receipt/audit
checks. A shared founder session delegates the configured service key, so its audit
identifies that key, not an independently authenticated human. TLS termination,
secure host/network settings, actor-specific identity and independent security review
remain deployment requirements. Closing a browser does not roll back bookkeeping.

## Evidence

44 new Node tests cover sessions/origins, strict query/body fields, fixed origins,
credential file restrictions, read-only setup, account/context and configuration
changes, bounded/redacted responses, historical request queries and no automatic POST
retry. A Python regression verifies all authenticated observations identify the tenant.

Four new real-browser scenarios cover confirmed recovery, a deliberately lost POST
response followed by reload/status inspection, a 390px mobile failed refresh, and
corrupt local request tracking. The fixture runs the actual FastAPI handlers,
reopened PersistentApiKeyRegistry, durable operator audit and institutional bridge
against disposable paper stores. One original fill remains one fill; accounting
moves from 100000 cash to 99000 cash plus 1000 securities cost and retains its halt.
No paid model, real brokerage connection or production account participates.

The browser CI job installs the existing development dependencies so the real Python
API fixture can run. No earlier tests, assertions, retry settings or acceptance gates
are removed. The initial new browser helper incorrectly assumed reload omitted the
existing Activity query parameter; that test-only URL assertion was corrected. Its
failed log and screenshots are retained separately from final certification.

Exact commit, complete suite results and configured CI evidence are recorded in the
PR. These tests are not human UAT, independent security certification, target-host
acceptance or evidence of AI profitability. M01 risk release, M03 scheduled execution,
source/model lineage, coordinated deployment/recovery and other roadmap gates remain.
