# Item 2: cached-history exceptions must not interrupt protection telemetry

## Status

DRAFT continuation of PR #133, based on `0e4fd70b44cd73a36518a557800a330611079350`.
This patch repairs a reproducible exception path. It does not complete Item 2,
prove availability of real market history, certify a deployed host, or authorize
merging, deployment, watchlist expansion or MCX admission.

## Reproduced behavior

Two new offline cases first populated the real DailyHistoryProvider cache with
synthetic daily bars and used the real runner's protection_tick to persist ready
telemetry. Injecting OSError or RuntimeError from cached() then escaped through
DailyCloseHistory.readiness into PilotTelemetry and out of protection_tick.
The original result was **2 failed / 14 passed**. These were fault-injection
reproductions, not errors observed on the deployed pilot.

The repair adds those two exception types to the existing readiness error boundary.
The result remains dataReady=false with reason history_invalid_or_stale. Configured
arming is not relabelled as real-data readiness. Both cases now persist unavailable
history telemetry without another feed call. No exception message is copied to the
published payload. No limits, provider selection, mapping, order routes or calendar
rules change. KeyboardInterrupt/SystemExit are not swallowed.

## Further passing offline boundaries

Seventeen added ordinary cases cover noncallable history, loss of both required
inputs, invalid freshness budgets, a naive observation clock, a future timestamp
after the same day's completed session, clearing observations after failed fetches,
prior-day/future cache boundaries, errors from cached reads, file-based map validation,
non-string map keys, correct interval counting and the Warden's actual refusal path.
None uses provider credentials or actual external data. Existing refusal tests and
the original 22-case preflight acceptance suite remain unchanged.

## Executed verification

Fresh baseline: **13 failed, 1790 passed, 2 warnings, 14 subtests passed in 206.02s (0:03:26)**.

Candidate: **13 failed, 1807 passed, 2 warnings, 14 subtests passed in 165.67s (0:02:45)**.

The exact 13 JUnit failure identities match: inherited Mac deployment-portability
cases, not new failures. No ordinary tests were skipped or waived. The combined
risk/preflight set passes **112/112**. The 466 selected source/test/script/config
hashes remain unchanged across certification. Ordinary counts exclude 14 subtests.
Ruff src/tests and whitespace checks pass. /usr/local/bin/ruff is absent on this Mac;
the absolute existing project virtual-environment interpreter was used instead.

Exact evidence is retained in docs/evidence/pilot-risk-cache-exception-suite.json.
A new published-head CI run is required; old green jobs do not certify this patch.

## Required sabotage: explicitly not complete

A 51-case source-guard inventory was prepared and each replacement anchor was
checked for a unique match. The attempted isolated runner creation/execution was
blocked by a tool safety check before execution. That operation was not retried
through another route. The runner is absent and no risk guard was removed in the
working repository or a running service. **Executed: 0; certified: 0.**

The inventory in docs/evidence/pilot-risk-guard-inventory.json is review material,
not a passing test report. In particular, removal of the two new cache-error handlers
has NOT been demonstrated to fail its designated regression in an executed mutation
campaign. Fault injection alone is not the requested guard-removal certification.
Keep the PR draft until that certification is genuinely completed.

## Remaining operational boundaries

No new public-history request was made here. The earlier zero-record/rate-limited
preflight is not replaced with manufactured bars or treated as a live-host diagnosis.
Usable real records, actual-host risk telemetry and covered-exit acceptance remain
unverified. The owner must merge Item 2 before Item 3 can start. No deployment,
service restart, broker order, paid model call, credential read or live-money setting
change was performed. No claim of complete risk coverage or trading profitability.

## Concurrent update preserved

Before publication, the branch advanced to `f5bcfc8e6f22f0d81ad66fb42cb8c651b9fa5839`.
Its three new files add 22 peer regression cases and documentation, with no filename
overlap or production-source change. They were preserved by a normal fast-forward
of the local parent, not a merge into main. All previously tested source bytes remain
identical; the combined focused set passes 134 cases. The 22 peer cases are not
attributed to this repair. The 467-file combined source/test/config snapshot is frozen.

Separate full integrated-parent and candidate runs were started and are pending at
this publication checkpoint. Their final results and the new exact-head CI belong
in the PR's current review record; no result is inferred here. Required portfolio-risk
mutation certification remains unexecuted regardless of ordinary CI results.
