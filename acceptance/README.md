# Blocking institutional risk acceptance

This directory preserves the previously uncommitted acceptance audit from
`tests/test_institutional_edge_authority.py` in its original isolated worktree.
The copy is byte-for-byte identical; no assertions or tested limits were relaxed.

Run explicitly from the repository root:

```sh
TRADING_LIVE_MONEY_ACTIVE=false PYTHONPATH=src:tests \
  python -m pytest -q acceptance/test_institutional_edge_authority.py
```

Current result: **15 failed, 2 passed**. This is a blocking acceptance failure,
not an expected-failure waiver and not evidence of 100% platform readiness.
The normal Python job still uses pytest's `testpaths = ["tests"]`. A separate
`institutional-risk-acceptance` CI job now executes this file explicitly and publishes
its JUnit report. Any failing case makes that job fail; no success override, skip,
or expected-failure marker is used. Normal-suite success must still be reported
separately from this blocking acceptance job. GitHub branch-protection settings
are not changed by adding a workflow job.

Findings: modeled-loss allowance across sliced parent orders; after-cost Kelly
sizing; complete projected factor-book identity; finite/nonnegative strategy
exposure. Covered exits must continue to work without entry-only evidence.

The previous production-code repair was tool-blocked. Publishing its reproducer
is not a retry of that repair. No real broker call, market evidence or trade occurs.
These assertions model synthetic acceptance requirements, not guaranteed loss bounds.
