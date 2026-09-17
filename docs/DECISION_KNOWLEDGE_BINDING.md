# Decision-time knowledge integrity

Continuation of draft PR #126 from `050a16ae8a87882f14cc477043d7a13f962d6628`.
This changes the optional Atlas knowledge-input boundary, not the previously blocked
edge/coordinator risk implementation. No running daemon, source permission, actual
training run, model approval, market admission or live execution is changed.

## Reproduced gap

Atlas accepted a context before its selection time, days after its source freshness
limit, and with an arbitrary nonhexadecimal 64-character selection digest. Validation
at initial routing was not repeated when Atlas consumed the context. The context
retained neither the decision time nor its grant snapshot; its digest also omitted
observation timestamps and source policy. Its collection inputs could remain mutable.
The initial new 29-case test run had 25 failures and 4 passes, including expectations
for the new validation API; those are not 25 distinct defects.

## Bound selection

New contexts use `pramana.decision_knowledge.v2`. Their digest covers the exact UTC
selection instant, ordered required categories, selected item identities, observed and
available timestamps, content hashes and references, and the complete declarations
of the source grants used for those items. Actual content bytes are checked again.
Records, required categories and grants are defensively copied. Equivalent timezone
representations of one instant produce the same canonical selection payload.

The context can reproduce its complete selection payload for independent hashing.
Changing the records or grant declarations while retaining an old digest refuses.

## Consumption-time enforcement

Atlas validates supplied knowledge before deterministic consensus or model inference.
Invalid explicit input holds even when knowledge is optional; it is not silently
ignored. The rejected bundle is excluded from decision provenance and model calls.
Direct context rendering and provenance methods also validate their retained binding.

Each selection is for exactly one decision instant. A later decision must request a
new router selection, even if some records are still fresh. The actual consumer time
must be timezone-aware and match the captured instant. The stored policy is replayed,
so a correctly rehashed context with future/stale data, unverified rights, missing
sources or duplicate grants still refuses. Required-category checks remain enforced.

Legacy three-field contexts lack the new time/policy binding. They remain representable
but cannot become usable decision evidence by inference. Existing historical records
are not rewritten or given invented policy/time metadata. Decisions supplying no
knowledge under the legacy optional policy retain their prior behavior.

## Evidence and limits

`tests/test_decision_knowledge_binding.py` contains 38 synthetic cases. Together with
existing knowledge, training, outcome, Atlas and candidate-registry tests, the focused
set passes 175 cases. Exact full-suite, guard-removal and CI results are retained in
the PR checkpoint. No real market data or paid model call is used in these tests.

The selection hash checks declared local consistency, not provider authenticity,
licensing, model quality or an authenticated access grant. A caller controlling the
whole process can manufacture declarations; this is not remote attestation.

Source-grant revocation outside the supplied controller, instrument/subject attribution,
provider qualification and actual daemon knowledge-provider wiring require their own
trusted inputs and acceptance. A same-instant bundle does not prove its timestamps
were supplied truthfully, and these checks do not guarantee prompt-injection immunity.
No source grants are expanded and no missing data is replaced with invented evidence.

The previously reproduced trading-risk defects remain separate: whole-parent modeled
loss allowance, cost-adjusted Kelly sizing, complete projected factor-book validation
and finite/nonnegative strategy-exposure inputs. Their production code and acceptance
assertions are unchanged here. The existing failing risk CI job remains enabled.
Full institutional-daemon assembly, reviewed migration/recovery, segment/settlement
coverage, the Mac deployment failures, authentic feeds, intended-host burn-in/restore,
alerts/security/human acceptance and forward after-cost performance remain open.
