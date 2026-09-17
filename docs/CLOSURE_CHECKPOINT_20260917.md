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

## AI knowledge/training boundary continuation

Base: `ac3ee5f0b2eb3af56bfeb6ec1b49e73c415e2a56`.

| Requirement | Engineering result | Remaining boundary |
| --- | --- | --- |
| Knowledge permissions | Strict enum/boolean/integer inputs; frozen scopes; read-only controller grants | Grants are operator declarations, not provider-rights authentication |
| Prompt metadata | Control/line separators rejected; source/reference JSON quoted | Not general prompt-injection immunity or default-daemon knowledge rollout |
| Training preflight | Run metadata validated before trainer invocation | Trainer behavior and raw dataset authenticity remain external |
| Dataset provenance | Full declared dataset contract hash retained beside artifact hash | Legacy missing binding stays unknown; no promotion authority |

Two new test files add 74 synthetic cases; focused AI/Atlas/learning tests pass 88/88.
Fresh Mac baseline is 1730 passed + 13 failed; candidate is 1804 passed + the exact
same 13 deployment failures, compared by JUnit identities. All 416 candidate Python
files are unchanged through full certification. Removing the plane guard, trainer
preflight or dataset binding fails the corresponding regressions; all were restored.

The separate prior edge-authority suite still reports 15 failures / 2 passes and is
not in collected CI. The four risk-code findings remain unrepaired/tool-blocked.
No hidden green claim covers that suite; no risk or coordinator source was changed.
Exact published commit and Linux CI evidence are recorded in the PR discussion.
See `docs/AI_KNOWLEDGE_TRAINING_BOUNDARIES.md` for scope and limitations.

## Candidate registry continuation (from 05134b2)

New paper-approval events require a passing recorded assessment, matching candidate
identity, exact assessment digest and a named reviewer. Replay independently verifies
stage history, timestamps, declaration types and the persisted assessment. v1 approvals
without this binding require review; history is never silently upgraded.

Local cooperating readers/writers use file locking; partial/ambiguous JSON refuses.
Forty-nine synthetic regression cases were added, with 137 focused AI tests passing.
No actual model was trained, approved, deployed or granted new capabilities.

The prior 17-case institutional risk acceptance audit is now preserved byte-for-byte in
`acceptance/test_institutional_edge_authority.py`, with its explicit command in the
adjacent README. It still reports 15 failures / 2 passes. Existing configured CI does
not collect that directory; neither testpaths nor assertions were changed or waived.
The earlier blocked risk-production repair was not retried. That acceptance and the
existing daemon, migration, deployment and external-evidence gates remain release blockers.

Exact commit, normal-suite results and remaining Mac failures are recorded in the PR
certification; normal-suite success must not be represented as risk-acceptance success.


## Secret-scan closure and CI risk-acceptance visibility

The history finding in token-renewal evidence was independently verified as the exact
SHA-256 of its corresponding committed script, not a credential. One historical
fingerprint is exempted; full history/current-tree scanning and synthetic detection
checks remain active. Safe location-only CI summaries now make later findings diagnosable.

The existing 17-case institutional risk suite now runs in its own non-waived CI job.
It remains 15 failed / 2 passed, with unchanged assertions and unchanged risk production
code. This is visibility and enforcement in CI, not repair of the prior blocked changes.

Local new scanner/gate suite: 18 passed. Fresh normal Mac baseline: 1853 passed / 13 failed;
candidate: 1871 passed / exactly the same 13 deployment failures. All 437 selected
source/test/script/config files were unchanged through full certification. Two guard
removal checks failed as intended; the original source was restored to that manifest.
Exact published head and Linux CI results belong in the PR certification checkpoint.
See docs/SECRET_SCAN_CLOSURE.md. No deployment, merge or live-money activation.

## Decision-time knowledge validation continuation

Baseline: `050a16ae8a87882f14cc477043d7a13f962d6628` on existing draft #126.
Only `learning/router.py` and the Atlas knowledge-consumption checks change in
production. The four previously blocked trading-risk repairs are not retried.

The new v2 context binds the UTC selection instant, complete item metadata including
observed time, source-grant declarations and required categories. Atlas revalidates
before consensus/inference; invalid explicit input holds and is excluded from model
calls/provenance even when knowledge is optional. Legacy unbound contexts are not
retroactively qualified. See `DECISION_KNOWLEDGE_BINDING.md` for trust limitations.

38 new synthetic tests; focused knowledge/Atlas/learning/candidate suite: 175 passed.
Fresh Mac baseline: 1871 passed / 13 failed. Candidate: 1909 passed / exactly the
same 13 deployment failures, compared by JUnit failure identities. All 439 selected
source/test/script/configuration files were stable during certification. Removing
consumer-time, selection-digest or content-byte checks made their behavioral tests
fail; after restoration the 175-test suite and Ruff passed. Actual history/tree
secret scans also passed. Published SHA and GitHub CI are recorded in the PR.

Risk acceptance remains 15 failed / 2 passed; its production code, assertions and CI
job are unchanged. No running-daemon change, new source grant, real model training,
model promotion, deployment, merge or live-money activation occurred. The full
institutional runtime and external acceptance/forward-performance gates remain open.

## Mac deployment portability closure

Continues #126 from `a7fcf09275d5556fd0db74625f8ac58bce6b4e84`.
Fresh full Mac baseline: 1909 passed / 13 failed. Candidate full normal suite:
1954 passed / 0 failed, with 14 additional subtests and no skips. The 13 original
failure identities all belonged to the deployment-script suite. Its 19 original
cases now pass alongside 32 new cases (51 focused), with all original test
functions/acceptance assertions preserved; only the Docker stub's portable final
argument handling changed in the existing test file.

The script replaces Bash-4-only arrays and GNU-specific sed stamping; validates
service/container inspection results; and preserves private environment bytes,
mode and ownership through a flushed temporary file and replacement. Controlled
exceptions and abrupt subprocess exits occur only in disposable test hosts.
New Ubuntu/macOS CI jobs explicitly use /bin/bash and stubbed Docker. Actual
published-head job results are recorded in the PR certification comment.

No source under src/, trading-risk acceptance assertion or risk CI job changed.
The risk acceptance still reports 15 failed / 2 passed and remains blocking.
No merge, deployment, real environment rewrite, daemon restart or live-money
activation occurred. This closes the named Mac portability failures, not full
rollout, rollback, hardware durability or trading/platform acceptance.
See `docs/DEPLOYMENT_PORTABILITY_CLOSURE.md` for scope and limitations.

### Final portability candidate after main refresh

Main advanced to `9bf0c29a042502303f0afdee8229ebbd9382a84f` (#131/#132) before
publication. Its specialist freshness updates and exact scanner exception are
retained. The deployment implementation is isolated in commit `0f59bb4`.
A first combined run recorded 1955 passed / 2 failed; both failures were telemetry
fixture quotes aging against the wall clock. The fixture now pins the already
injectable daemon clock; original test functions/assertions are unchanged. Two
new delayed-collection regressions reproduce and protect this correction.

Final combined Mac normal suite: **1959 passed / 0 failed**, two warnings,
14 additional subtests, 121.11 seconds. All 439 selected executable/configuration
files remained unchanged during certification. The count includes 32 deployment
regressions, two clock-fixture regressions and three newly merged upstream cases.
The separate institutional risk acceptance remains **15 failed / 2 passed**.
Actual published-head Linux/macOS CI results belong in the PR certification record.
No target-host deployment, main-branch merge, live activation or production freshness
limit change was performed by this continuation.

## Reviewed paper-OMS recovery and current-main integration

Continues draft #126 from `8bd646f97a8e145c7b2718c0e9d0c798c97131d8`, retaining
the previously interrupted recovery work and including current main
`13bbdec81fe59332513260b746d51ec75bcf565d` (#130 research publisher).
All incoming publisher files match main byte-for-byte. The workflow conflict
retains the existing stronger two-phase scanner diagnostics and the blocking
risk-acceptance and cross-platform deployment jobs; no failure is waived.

`PaperOmsRecovery` inspects and mirrors only a provably committed bound NSE cash
paper fill into OMS. It requires the exact reviewed plan hash and durable halt,
validates immutable intent/receipt/release/configuration evidence, and atomically
records the fill, append-only recovery audit and schema-compatibility version.
It never resubmits, changes paper cash/positions, posts accounting or clears the
halt. Missing receipts, conflicting states and unqualified evidence remain blocked.
The Python API is not an authenticated console or unattended repair loop.

The finished file adds 41 synthetic recovery cases. The focused recovery/runtime/
OMS/broker-reconciliation set passes 95. A first resumed full run found a clock
serialization race (2164 passed / 1 failed); deterministic regressions now enforce
implicit time after locking while retaining explicit historical-time rejection.
Final combined Mac suite: **2167 passed / 0 failed**, two warnings and 14 additional
subtests (273.03 seconds). Risk acceptance remains **15 failed / 2 passed**.
All 485 selected source/test/script/configuration files were unchanged. Three
isolated in-memory guard removals failed their tests; source files were never mutated.
Actual published-head GitHub results are recorded in the PR certification comment.

No risk implementation or acceptance assertion was changed, and no real account
recovery, merge into main, deployment, model promotion or live-money activation
occurred. See `docs/PAPER_OMS_RECOVERY.md` for scope and remaining release gates.
