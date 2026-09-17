# Token-renewal integration refresh

The prior ready-for-review PR #129 conflicted with current main in two files:
`.github/workflows/ci.yml` and `.gitleaksignore`. Main added secret-location diagnostics
and the same verified checksum exception; renewal already supplied the more thoroughly
tested blocking two-phase scanner. The refreshed branch retains that exact scanner
workflow/helper and one exact fingerprint, incorporating all other current main changes.
No security rule was relaxed and no new fingerprint or runtime guard was added.

Parent: `694ad93b97c01197e1985f9977ea60ff3a223617`.
Main integrated: `13bbdec81fe59332513260b746d51ec75bcf565d`.

Full Mac baseline: **1671 passed / 13 failed**, two warnings, 14 subtests passed.
Refreshed full Mac suite: **1841 passed / the same 13 failed**, two warnings,
14 subtests passed. Failure identities compared exactly. The additional 170 tests
come from owner main, not new tests in this refresh. No test was skipped or waived.
All 464 selected file hashes stayed unchanged across verification. All five
non-security CI jobs match current main structurally; the security workflow matches
the previously verified renewal head byte-for-byte. Runtime renewal source is unchanged.

No new guard requires a new sabotage campaign here. Existing campaign evidence remains
in the earlier renewal documents and PR discussion; those campaigns are not claimed as
rerun. This refresh requires its own exact-head CI, never the older green result.

No PR was merged into main, no deployment/restart/login occurred, no production
credentials were read, and live-money settings remain untouched. Interactive owner
login/2FA, intended-host renewal/recreation and verified pre-open alert delivery remain
separate acceptance requirements. The inherited 13 Mac portability failures remain open.
