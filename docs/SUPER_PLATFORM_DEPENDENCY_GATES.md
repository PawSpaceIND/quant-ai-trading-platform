# Super-platform dependency gates

The 33-capability register is a dependency-aware evidence checklist, not a certificate of
profitability, data authenticity or production readiness. Nonempty evidence references must
still point to separately reviewed, exact-version artifacts. A string or populated object is
not authenticated merely because the checklist accepts it. Bare flags and numbers refuse.

Engineering completion requires the capability's own engineering references and the
engineering completion of every upstream dependency. External completion reports only the
capability's own external references. Launch completion additionally requires every upstream
capability to be launch-complete. A missing live feed or broker capture therefore propagates
to downstream launch status without erasing genuinely completed unit-test work.

Reports name both blocked engineering and blocked launch dependencies, separately from
missing direct references. Evaluation follows dependency edges, not list order. Cycles,
unknown dependencies, duplicate IDs, empty registers and capabilities without engineering
proof requirements refuse instead of producing a green report.

This patch adds no new trading permission and attaches no fabricated external evidence.
MCX admission, actual contract-note reconciliation, target-host burn-in, authentic forward
strategy/calibration evidence, settlement and independent acceptance remain separate gates.

## Continuation verification

Nine new negative cases reproduced incorrect success on the previous implementation.
The final dependency suite adds 15 cases (including order-independent evaluation and
invalid-register checks). The combined targeted set covering the coordinator, OMS/broker,
contract-bound orders, margin integration and the closure register passes 96 tests.
The full cumulative Mac tree passes 1,567 tests with the same 13 deployment-portability
failures as the broker hardening lane. JUnit failure identities match exactly.
Ruff across `src tests` and `git diff --check` are clean. No gate is weakened.

The integrated broker prerequisite is PR #127 commit
`debe4abf89125fe8ea6055ca16f6c97afb3f9515`, independently certified by GitHub CI run
`35133538298`: all six jobs passed and Linux pytest reported 1,443 passed.
These counts are engineering evidence, not external-market qualification.
