# Item 1: approved cadence integration and deterministic protection test clock

This refresh incorporates owner-merged #134, main `58a3d72a6fafbd17df97bccb78943a30be06df48`,
into the existing renewal PR. Its five files have no overlap with the renewal patch.
All incoming files match main byte-for-byte. The tick-cutoff implementation belongs
to #134; this is not a new history-data source, portfolio-risk policy or order route.

A full combined Mac run exposed a pre-existing test clock problem: the protection
fixture stamps quotes at collection but later checks their freshness against wall time.
The same file passes when run immediately. Two deliberate collection delays (five
minutes and one day) reproduce failures on the old fixture and pass on the reused
peer fixture from `8bd646f97a8e145c7b2718c0e9d0c798c97131d8`.
That one test file is copied byte-for-byte. It pins the existing injected daemon clock
to the quote instant; all original test-function ASTs and assertions remain identical.
The intentionally stale 200-second quote still fails freshness, and the rebased case
still reports suspension. No production clock or freshness budget is changed.

The initial combined run reports 17 failed, 1881 passed and one error. It includes
13 known Mac deployment failures, two delayed-clock failures, two SQLite disk-I/O
failures and a temporary-directory setup error. Exact initial failures are retained.
No definitive cause is asserted for the filesystem errors. The final run uses separate
new temporary directories and the fixed fixture, with no skips or expected failures.

Final Mac result: **1888 passed, 13 failed, two warnings, 14 subtests passed**.
The 13 failure identities exactly match the already-known deployment Bash/sed cases
on the renewal branch. Their fixes remain on Item 2, not in this refresh or main.
The prior recorded parent result was 1841 passed / 13 failed; this refresh brings in
45 owner-main cases plus two reused fixture regressions, not 47 newly authored cases.
All 19 owner-main cadence mutation tests pass in the full run. They do not certify
the separate blocked portfolio-risk inventory, which was not executed here.

All 465 selected source/test/script/config files remain hash-identical through final
verification. Ruff passes using the absolute existing venv interpreter because the
requested `/usr/local/bin/ruff` is absent. The new exact-head CI still must run after
publication; old green checks do not certify this new integration.

No credentials or production environment were read, no source provider was changed,
no live-money code was added, and no host service was restarted. The owner alone
merges and deploys. Daily login/2FA, intended-host file compatibility, consumer refresh
and actual alert delivery remain acceptance gates. Item 3 remains blocked pending
owner merge of Item 2. Full four-item closure is not claimed.

Machine-readable evidence: `docs/evidence/zerodha-cadence-main-refresh.json`.
