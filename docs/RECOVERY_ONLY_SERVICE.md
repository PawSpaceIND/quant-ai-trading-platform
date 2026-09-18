# Dedicated recorded-bookkeeping recovery application

Parent: #154 at `666b200782ff8c656fb65cc448b8bce843bb951b`.
This closes service-surface isolation for the existing recovery workflow, not the
complete deployment, identity, source or trading-readiness milestones.

## Why a separate application

The earlier browser fixture assembled the general FastAPI application with recovery
operations enabled. That application deliberately includes older paper-trade,
capital and Atlas endpoints, with its older API-key model. Recovery-specific scopes
do not redefine permissions for every legacy endpoint. This change does not rewrite
that existing contract or claim that all of its routes were recovery-only before.

`quant_ai.api.recovery_app.create_recovery_app` builds a separate application. Its
only production routes are a limited GET health response and the three existing
GET/POST bookkeeping-recovery routes. It never constructs TradingService or an Atlas
coordinator, installs trade/model/account-inspection routes, enables public API docs,
issues credentials or creates a trading account. Existing create_app is unchanged.

## Explicit server selection

The host supplies an already opened PersistentApiKeyRegistry and a nonempty mapping
of tenant IDs to already configured InstitutionalRecoveryOperations. In-memory keys,
mismatched tenant labels and malformed selections refuse. The mapping is copied and
read-only within the application. Neither browser parameters nor later mutation of
the caller's dictionary can select a different runtime.

Ownership remains with the host: app construction and shutdown do not construct,
close, restore or migrate these stores. Borrowed runtimes and audits retain their
existing identity checks and locks. This is a selectable ASGI factory, not automatic
production provisioning, a daemon launcher or a new credential-issuance endpoint.

The existing three recovery handlers are reused unchanged. They enforce exact
saved-context confirmation, separate read/apply scopes, durable audit intent,
idempotent historical outcomes and credential revalidation after the runtime lock.
The dedicated authenticator rereads the persistent key grant, checks configured
tenancy, and serializes its tenant rate-limit admission. Default limit: 120 per
minute. Its 10,000/minute and 32-tenant validation ceilings are not performance claims
or operational recommendations. Enabling a live-money flag holds authentication;
this app never has a live or paper order-submission route.

## Bounded request surface

Recognized recovery POST bodies are limited to 2 KiB before JSON parsing, including
chunked bodies. The envelope checks declared lengths, rejects conflicting length
or media headers, requires JSON and applies a five-second body-read deadline.
Malformed, oversized, timed-out and disconnected requests do not reach source
reconciliation. Disconnection before request completion produces no operation.
This deadline is for receiving the request, not cancelling committed bookkeeping.

All HTTP results, including health, authorization, 404 and body errors, carry
no-store and nosniff. Validation uses the existing redacted errors; body failures
return fixed codes, never submitted data. Redirects for missing trailing slashes and
public documentation routes are disabled. No CORS trust or external TLS termination
is silently added. Health says only that this recovery application is serving, not
that every account, audit, broker or credential is healthy or live-ready.

## Integration and regression evidence

38 synthetic Python cases exercise actual handlers and durable credentials, surface
inventory, absence of legacy constructors, confirmation/idempotency, default/read
permissions, revocation/expiry/queued revocation, tenant selection, missing credential
storage, mapping immutability, borrowed-store ownership, rate limits, bounded bodies,
malformed/slow/disconnected streams and preservation of halts and risk reservations.
The two initial failures specified the absent dedicated factory; they were not
operational incidents. Final exact-commit results are recorded in the PR.

The existing real-backend browser fixture now assembles this production factory
instead of general create_app. It still seeds disposable paper accounts and synthetic
credentials separately. All ten earlier browser journeys remain unchanged. An
additional journey proves that a valid recovery key can inspect its programme but
cannot reach trade/model/legacy account routes; source state stays unchanged.
The fixture-only /fixture/observations route is added only by the test launcher and
is not part of the production factory.

## Remaining boundaries

Existing source-runtime construction, secure credential provisioning, independent
human identity, TLS/gateway selection and actual host setup remain operator work.
The service uses local possession credentials and in-process synchronization, not
cross-host authorization or independently signed broker ownership. Trusted host
code can still build the broader app; this factory does not sandbox malicious code.
Rate/load, process/socket and filesystem acceptance still require the intended host.

This adds no risk release, scheduled execution, order retry, halt clearing, model
promotion or automatic backup. No real account or credential is changed. M01/M03
and the remaining 12-milestone requirements remain open. Synthetic tests establish
this route boundary, not human acceptance or a profitable trading edge.

## Certification notes

Four isolated in-memory guard removals reproduce the broader-route, unbounded-stream,
mutable server mapping and nondurable-credential-selection failures without editing
certified source. The initial two specifications failed because the dedicated factory
was absent. Existing test assertions are unchanged; only the browser fixture's app
assembly is switched to the new production factory.

A local build attempt reused dependencies through an out-of-tree node_modules symlink,
which Turbopack refused. Only that newly created link was removed, and the unchanged
lockfile was installed into the isolated worktree before default build/browser checks.
No bundler restriction, package version, workflow or product setting was changed.
The first setup-failure log remains separate from final results.
