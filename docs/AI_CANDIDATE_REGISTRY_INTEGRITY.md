# AI candidate approval and registry integrity

Continues draft PR #126 from `05134b2b22e99ce18ee628907fbf77d7d1c16dde`.
This is an independent learning-governance repair. It does not change the previously
blocked edge, factor-book, strategy-exposure or coordinator production code. No actual
model is trained, promoted or deployed by this change; all test registries are synthetic.

## Reproduced gaps

A candidate could reach PAPER_APPROVED with only a reviewer name and arbitrary digest:
`transition_candidate` did not require or bind the result of `assess_candidate`.
Registry replay checked the JSONL hash chain but accepted impossible stage transitions,
wrong prior stages, invalid evidence digests and records claiming live authority.
Malformed candidate evaluation inputs could also escape assessment validation.
The first 28 acceptance cases recorded 27 failures and one pass, including new API
expectations; these are not 27 distinct defects. Later tests reproduced extension of a
record missing its final newline and acceptance of duplicate JSON keys.

## Approval evidence

New stage records use `pramana.ai_candidate_registry.v2`. PAPER_APPROVED requires a
named reviewer, CandidateEvaluation, StrategyEvidence and an explicit or default
PromotionPolicy. The selected policy and complete evaluation/strategy inputs are stored
with the recomputed assessment. `candidate_assessment_sha256` provides the exact digest
required as `evidence_sha256`. Candidate identity, digest, probability/strategy assessment
and no-live-authority verdict must all agree before append. Raw stage strings, malformed
identities, nonfinite metrics and noninteger counts refuse before writing approval.

This binds declared evidence, not authentic market performance. Reviewer text is not
reviewer authentication, and a locally computed hash is not a provider signature.

## Replay and local serialization

Replay validates every candidate transition from its previously reconstructed state,
reviewer/evidence fields, monotonic timestamps and literal false live authorization.
For v2 approvals it reconstructs the typed inputs and reruns the assessment using the
recorded policy. A correctly rehashed but semantically invalid approval still refuses.
Readers reject duplicate keys, incomplete final records and nonfinite JSON constants.

The registry holds a nonblocking shared/exclusive file lock through read or through
read-validation-append. Competing cooperating local writers cannot append from the same
prior state. Busy operations refuse rather than wait indefinitely. New registry files
are private (0600); symlinks, hardlink aliases and nonregular files refuse. Missing-file
reads do not create files. Existing history is never automatically truncated or repaired.
The shared evidence-log implementation and its other users are unchanged.

This uses Unix `fcntl.flock`, documented at:
https://docs.python.org/3/library/fcntl.html
Locks are advisory and scoped to cooperating processes on local supported storage.
They are not distributed leases, source authentication or proof of hardware durability.
Partial writes require explicit review; process termination after a complete commit
retains that record and does not authorize a duplicate transition on retry.

## Compatibility

Well-formed v1 nonapproval transitions remain replayable without rewriting their bytes.
A v1 PAPER_APPROVED event lacks the new assessment binding and requires reviewed
migration; it is not silently relabelled as a qualified v2 approval. No migration tool
or runtime-model selection hook is introduced here. Prior automated tests now supply
actual synthetic assessment inputs to their existing positive approval case.

## Verification and unresolved acceptance

The new candidate registry file contains 49 synthetic cases. Combined existing and new
AI/learning coverage passes 137 tests. Cases cover unassessed approval, failed or wrong
candidate evidence, rehashed invalid history, drifted policy/digests, malformed metrics,
concurrent threads/processes, partial JSON, duplicate keys and an abrupt process exit
after the log commit. Exact-head full-suite and guard-removal evidence is in the PR.

The previously local risk acceptance file is retained byte-for-byte at
`acceptance/test_institutional_edge_authority.py`. It still reports 15 failures and
2 passes. Existing pytest configuration collects `tests/`; it was not changed to hide
or waive failures. The acceptance command and its blocking status are documented in
`acceptance/README.md`. Green configured CI does not mean this acceptance passes.

The four unresolved findings remain edge-loss budget enforcement, cost-adjusted Kelly
sizing, complete projected factor-book identity and finite/nonnegative strategy exposure.
Their production-code repair was previously tool-blocked and is not retried here.
Full institutional-daemon assembly, source/model artifact qualification, authenticated
review and approved migration, segment admission/settlement, Mac deployment portability,
target-host/restore/alerts/security/human acceptance and genuine after-cost performance
remain release gates. No model approval or profitable trading evidence is manufactured.
