# Closure checkpoint — broker namespace and decision identity

This checkpoint records specific engineering closures, not a percentage of platform or
launch readiness. PR #127 is the broker lane; draft #126 is cumulative integration.
No changes in this continuation were made to PR #112 or to main. No PR was merged.

| Requirement | Verified engineering evidence | Boundary still open |
| --- | --- | --- |
| Imported execution identity | 17 namespace/legacy tests in test_broker_fill_namespace.py | Real account evidence and full routing model |
| Decision contract propagation | 26 tests in test_decision_contract_propagation.py | Opt-in rollout and release-manifest wiring |
| Exact parent recovery | Changed parent refuses; TWAP restart does not replay completed slice | All other cross-database crash windows are not certified here |
| Legacy safety | IDs/events unchanged; missing binding or parent refuses | Operator-reviewed migration and compatible rollback |
| Combined regression | Full source-hashed Mac run and required exact-head Linux CI | 13 Mac deployment-portability cases remain open |

## Release blockers retained

- Dependency review/merge order, including the existing margin prerequisite for MCX Part 4.
- MCX pilot admission, exact contract-to-margin/fee source linkage, authentic contract-note
  reconciliation and fresh qualified margin capture. No invented rates or reconciled notes.
- Qualified point-in-time feeds, corporate-action adjustments, instrument source coverage,
  derivative lifecycle/delivery calendars, options fees/spread-margin and settlement evidence.
- Complete decision-time broker routing identity, operator configuration and full default
  runtime integration; an optional bound request is not a claim that the running daemon uses it.
- Mac deployment portability. The previously safety-blocked separate candidate is untouched.
- Target-host sustained burn-in, off-host restore, alert receipts, security review and human UAT.
- Forward strategy and probability-calibration evidence after costs. No passing test count
  proves alpha, profitability, or eligibility to enable live-money execution.

The 33-capability register remains an evidence-reference/dependency checker, not an artifact
authenticator. No placeholder, synthetic capture or nearby module is treated as external proof.

## Subsequent committed-fill recovery checkpoint

The existing integration now includes the separately documented
`COORDINATOR_CRASH_RECOVERY.md` work. Twenty-seven new synthetic regressions cover
committed paper fills lost before OMS/program recording, abrupt subprocess termination,
exclusive dispatch claims, broker-owned receipts, historical cost-basis validation and
partial-fee recovery. This does not enable the default daemon or real-money execution.

No-receipt dispatch uncertainty stays RECOVERY_REQUIRED and blocks new coordinator
dispatch for the tenant; it is not permission to retry. Legacy receipt migration,
rollback support for the new state, independent-exit accounting integration and target-host
acceptance remain explicit open requirements. Exact source and CI evidence are on PR #126.


## Subsequent independent-exit accounting closure

The scoped coordinator path now mirrors committed protective exits into the existing
same-currency accounting journal, outside the emergency execution path. Durable
append-only exit obligations detect missing evidence; verified prior entries and exact
recorded cost/fees are required. New coordinator slices wait on unresolved accounting.
See `PROTECTIVE_EXIT_ACCOUNTING.md` for the deliberate single-currency/legacy boundaries.

Local evidence: 25 synthetic regressions, 126 focused passes, fresh baseline 1667/13 and
candidate 1692/13 with identical failure identities. All 412 candidate Python files were
unchanged during the full run. Removing mirroring, outer fee-posting atomicity or the
missing-evidence obligation guard made the relevant tests fail; restored code passed.
Exact published SHA and Linux CI are recorded in the PR certification comment.
Default-daemon rollout, reviewed migration/rollback, other capability and external gates
remain open; this does not grant live execution or establish a profitable strategy.

## Runtime contract-mode and manifest continuation

The existing ghost-daemon builder and its environment entry point can explicitly select
bound_v1 and a separate durable OMS. The Compose pass-through retains the legacy default.
The mode/scope/storage pin survives restart, is reflected in runtime manifests, and refuses
legacy relabelling, changed storage and unbound new risk. Bound entries require matched
release evidence and reconciled OMS history. Independent covered exits remain available.
Post-commit submission exceptions with durable OMS are uncertain, not fabricated rejections.
See RUNTIME_ORDER_IDENTITY.md and the exact-head CI comment for 38 added regression cases.
This does not switch the running daemon or assemble the institutional coordinator as its
main execution path. That assembly, accounting-wide restart/operations, reviewed migration,
qualified external evidence and the previously listed release gates remain open.
