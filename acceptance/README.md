# Blocking institutional risk acceptance

This directory preserves the previously uncommitted acceptance audit from
`tests/test_institutional_edge_authority.py` in its original isolated worktree.
The copy is byte-for-byte identical; no assertions or tested limits were relaxed.

Run explicitly from the repository root:

```sh
TRADING_LIVE_MONEY_ACTIVE=false PYTHONPATH=src:tests \
  python -m pytest -q acceptance/test_institutional_edge_authority.py
```

Baseline at #126 head `3cfd99dda203fd3688906fa22b900e4e25214f6b`:
**15 failed, 2 passed**. On `fix/post-126-risk-closure`, all **17 cases pass** after
the four production repairs. The acceptance file itself is byte-for-byte unchanged.
This is not an expected-failure waiver or evidence of 100% platform readiness.
The normal Python job still uses pytest's `testpaths = ["tests"]`. A separate
`institutional-risk-acceptance` CI job now executes this file explicitly and publishes
its JUnit report. Any failing case makes that job fail; no success override, skip,
or expected-failure marker is used. Normal-suite success must still be reported
separately from this blocking acceptance job. GitHub branch-protection settings
are not changed by adding a workflow job.

Findings: modeled-loss allowance across sliced parent orders; after-cost Kelly
sizing; complete projected factor-book identity; finite/nonnegative strategy
exposure. Covered exits must continue to work without entry-only evidence.

The original repair attempt had been blocked, and the reproducer was subsequently
published with its failures intact. The post-126 branch now applies the repairs in
an isolated paper-only working tree. No live broker transport, account configuration
or running daemon is changed. The exact repair and remaining scope are documented
in `docs/POST_126_RISK_CLOSURE.md`. These assertions model synthetic acceptance
requirements, not guaranteed loss bounds or authenticated market evidence.
