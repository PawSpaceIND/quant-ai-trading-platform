# Item 2: approved cadence integration, test-clock repair and history handoff

This refresh incorporates owner-merged #134 at main
`58a3d72a6fafbd17df97bccb78943a30be06df48` into the existing risk PR, whose
parent is `85369172a1b03ed8dbd936652350118a1d4a879a`. The five incoming files have
zero overlap with the prior Item 2 changes and match owner main byte-for-byte.
The cadence change selects an eligible retained tick at the original cutoff; it
is not a new daily-history provider and does not qualify missing historical records.

## Test reliability repair, with a before/after reproduction

Initial combined full run: **1919 passed, two failed, two setup errors**. The two
failures are fresh/rebased protection fixtures whose module-collection timestamp
had aged during the preceding full suite. Running that existing file immediately
passes all ten cases, so a quick targeted pass does not invalidate the full failure.
The two setup errors involved a throwaway Git push and temporary-directory creation;
their messages are retained. No unproved root cause is asserted for those errors.

Reused only `tests/test_protective_state_telemetry.py` from peer commit
`8bd646f97a8e145c7b2718c0e9d0c798c97131d8`. That file is byte-identical to the donor.
The helper pins the daemon's existing injectable clock to the fixture quote time.
All original test/fixture function ASTs and assertions remain unchanged. No production
clock or freshness limit changes; the deliberately stale 200-second quote remains
stale. No other institutional or peer runtime code is imported.

Two deterministic collection-delay cases (five minutes and one day) fail on the old
fixture and pass on the donor fixture. These are normal regression reproductions,
not a new production-guard sabotage claim. The final full run uses dedicated fresh
TMPDIR and basetemp locations to isolate this invocation's temporary files.

## Final executed verification

**1925 passed, zero failures/errors/skips, two warnings, 14 subtests passed.**
The separate unchanged preflight acceptance suite passes **22/22**.
The recorded parent baseline was 1878 passed, zero failures. The increase is exactly
45 cases from owner main plus two existing peer fixture regressions, not 47 new
cases authored by this continuation. All 19 owner-main cadence mutation checks pass
inside the full suite. They do not replace the separately blocked portfolio-risk
campaign, which still has 51 planned and zero executed/certified cases.

All 471 selected source/test/script/config hashes remained unchanged during final
certification. Ruff passes through the absolute existing venv interpreter; the
requested `/usr/local/bin/ruff` remains absent. No test was skipped, xfailed or
weakened. New published-head CI remains required; an old green run is not sufficient.

## Remaining real-data work is explicitly not complete

`docs/PILOT_HISTORY_ACCEPTANCE_PLAN.md` records the actual integration gap, reusable
components, proposed explicit source-selection and evidence required at each stage.
It is a non-executable plan, not an adapter, provider response or host acceptance.
The blocked adapter/mutation writes and private monitor snapshot read were not
retried, split or routed through another tool. No rate-limit workaround was used.

No credentials, running service, production ledger, risk threshold, watchlist,
MCX admission or live-money setting changed. No new provider/model/broker request
was made. The owner controls PR merge and deployment; this only refreshes a feature
branch. Keep Item 2 draft: real qualified history, risk-guard verification, running-host
risk telemetry, covered exits and operator acceptance remain open. Item 3 waits for
owner merge of Item 2. Machine-readable results are in
`docs/evidence/pilot-cadence-main-refresh.json`.
