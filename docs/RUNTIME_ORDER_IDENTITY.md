# Paper-daemon contract-mode rollout

This extends the existing draft integration PR #126 from `e90219e7e7cbbcb0e46fab893dce38c337cd2c63`.
It changes source/configuration contracts only. It does not deploy, migrate the running
account, expand market admission, enable live money, or qualify any data/model source.

## Explicit startup selection

`build_ghost_runner` now accepts `order_identity_mode` and `oms_database`.
The environment builder reads `PRAMANA_ORDER_IDENTITY_MODE` and `PRAMANA_OMS_DB` before
constructing providers. Accepted modes are exactly `legacy_cash` and `bound_v1`.
Unknown, empty, boolean-like, or mistyped modes refuse rather than silently disabling a gate.

The default remains `legacy_cash`; existing deployments are not silently opted in.
`bound_v1` requires the existing pilot admission rules, a durable paper ledger and an
explicit distinct durable OMS database. Memory databases, same-file aliases, symlinks
and missing paths refuse. This remains the existing supported NSE INR cash pilot; it
does not open MCX, options, or a new currency/product routing path.

The selected Instrument snapshots flow through actual pipeline requests, Atlas proposal
construction, RiskWarden, durable OMS, paper fill evidence and protected exits. No registry
lookup at fill time reconstructs the missing identity. The builder now actually supplies
this pipeline choice and durable OMS instead of leaving the path available only to tests.

## Persistent rollout boundary

A new account can pin its exact instrument scope and selected OMS path digest in
`paper_runtime_order_identity`. The record is append-only. Rebinding a different scope,
mode or OMS database requires explicit reviewed migration; the startup path refuses it.
Legacy history is not relabelled, even when positions are flat. An unrelated existing OMS
for the same tenant also requires review. No historical approvals or receipts are invented.

The broker checks the pin inside its existing fill transaction, so an unbound BUY cannot
bypass the rollout contract through a direct broker call or after restart. Covered SELLs
still use the stored position identity; rollout metadata is not allowed to trap protection.
The optional recovery-table allowlist preserves this record in eligible replay bundles.
A path digest is not file authentication, a distributed lock, or an off-host restore proof.

## Manifest and entry safety

The runtime strategy manifest records the effective pipeline mode, pinned scope, and OMS
presence/path binding. Credentials and raw database paths are excluded. Mode or OMS
wiring changes invalidate the fingerprint. Bound mode requires a matched manifest, not
merely a manifest that exists; missing revision or unsupported components hold entries.

Bound-mode pre-submit checks verify durable OMS history, refuse unresolved orders and
compare recorded paper fills with OMS execution state. Replacing the OMS with an empty
file at the same path cannot erase those obligations. The entry hold engages the existing
kill switch; independent protective exits remain available and have regression coverage.

The durable swarm path no longer labels a submission exception as definite rejection:
an error can occur after commit. It records SUBMISSION_UNCERTAIN instead. This change
adds no automatic retry, cancellation, or repair authority. Operator-reviewed recovery of
those OMS outcomes remains required; it is not the coordinator's program-recovery API.

## Executable evidence

`tests/test_runtime_contract_mode.py` adds 38 synthetic cases. They cover mode parsing,
constructor and environment wiring, storage aliases, legacy/mismatched configuration,
manifest drift and missing release evidence, actual bound factory-to-OMS/fill provenance,
unresolved/replaced OMS storage, post-commit exceptions, and independent exits during holds.
The targeted daemon/manifest/pilot/replay/OMS/coordinator/protection set passes 149 tests.
Exact full baseline/candidate and published CI results are recorded in the PR discussion.

The existing manifest-mode defect was reproduced before implementation. Two further
behavioral tests demonstrated entries passing incomplete release evidence and pending OMS
state, and a post-commit exception test demonstrated a real fill being labelled REJECTED.
All records and prices are synthetic. No authentic broker order or profitable edge is claimed.

## Release boundaries still open

This wires the contract-bound mode and durable OMS into the existing daemon builder.
It does NOT replace the swarm daemon with the full institutional edge/allocation/accounting
coordinator. That larger assembly, daemon-wide accounting recovery, multi-currency routing,
reviewed migration/rollback and unattended uncertain-order resolution remain separate work.

No running environment was edited, no daemon restarted, and no PR merged by this change.
The Mac deployment portability failures, MCX/other segment admission, qualified data and
real broker/contract-note/margin/settlement evidence, target-host burn-in/restore/alerts,
security/human acceptance and forward after-cost performance still remain release gates.

Docker Compose now passes the explicit selector and OMS path to the ghost service;
its default is unchanged. `${VAR-default}` preserves an explicitly empty selector so
the builder can refuse it rather than convert it to legacy mode. The optional blank
OMS path is normalized to None; bound mode still requires a nonempty path. Compose
interpolation semantics: https://docs.docker.com/reference/compose-file/interpolation/
No Compose deployment was executed during this source change.
