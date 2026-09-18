# Learning feedback: analysis-cutoff continuation

This continues PR #138 on its existing branch. The published parent is
`a1848f5c267b19168b238b8333935215c57e64f6`; its incorporated main is
`5bacb52337351c41fe782ac848edf1ae33647614`. This is paper-only engineering,
not a deployment, model promotion, or claim of improved trading skill.

## Reproduced boundary and retained correction

A bound feedback refresh previously used the wall clock even after restoration
received an explicit historical cutoff. The swarm also did not pass its analysis
time to weighting. Three saved regression cases exposed future-outcome influence:
the frozen restoration case, synchronous swarm, and asynchronous swarm.

The binding now retains its optional upper bound. A refresh uses the earlier of
that upper bound and the supplied evaluation time. The two swarm paths pass the
real `AgentAnalysisRequest.observed_at`, not an invented `now` attribute. Normal
live bindings omit a permanent upper bound and advance with each actual request.
Only an explicit new restoration can widen a frozen binding.

The report observer has a separate clock: the report's decision window remains
the cadence cutoff, while feedback/drift observation and alert deduplication use
the actual daemon clock. Neither changes tick timestamps or market-session gates.

## Verification recorded in this continuation

The retained local patch was read before editing; it was not replaced by the old
partial disconnected draft. Two small lint defects in a saved test were repaired
without changing its assertions. Time annotations and formatting were confined
to the same attribution/swarm source files.

Nine additional positive boundary cases cover one second before, exactly at and
one second after closure; a later evaluation against a frozen binding; an earlier
evaluation against a later binding; advancing and reversing an unfrozen projection;
equivalent IST/UTC instants; naive-clock refusal; and report/observation separation.
The focused combined run passed **117 tests**. Ruff `src tests` and whitespace
checks passed. The absolute existing project virtual-environment interpreter was
used, not an assumed `/usr/local/bin/ruff` installation.

The full test suite and fresh published-head CI must both finish before marking
this change ready for review. Their actual final counts and commit/tree identities
are recorded on PR #138 after completion. The prior parent green run does not
certify this new tree. The previous exact-main full baseline is retained separately
as 2,551 ordinary tests plus 14 subtests; it is not represented as newly rerun here.

The previously tool-blocked additional guard-removal batch was not repeated.
The existing 12-case sensitivity module remains in the ordinary suite, with its
next-refresh anchor updated for the explicit evaluation-time argument. The saved
three failed-before / passed-after cutoff cases are regression evidence, not a
claim that an additional mutation campaign completed.

## Limitations and preserved ownership

The source authority is still the existing resolved journal and its recorded
exit timestamp. This boundary does not establish when an external fact first
became known. In particular, it does not manufacture a missing historical
knowledge-time or resolve arbitrary late-imported data into a point-in-time-safe
backtest. The append-only audit and recorded use basis remain separately inspectable.

The learning observer still requires an operator-selected genuine probability
journal and model/cost identity; raw NEUTRAL/confidence records are not converted.
Unconfigured or unusable evidence stays explicit. No forecast producer, training
service, automatic model activation, alert-delivery acceptance or forward edge is
established by these tests. Feedback refusal removes adaptive weights, not the
independent protective-exit loop.

No root daemon factory, broker ledger implementation, risk/history adapter,
Compose, CI, credentials, live-money setting, watchlist or running service is
changed. #133 and #136/#137 continue to own their respective work. The primary
Mac checkout and its seven unrelated modifications are preserved.
