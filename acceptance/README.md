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
Existing configured CI uses pytest's `testpaths = ["tests"]`; that configuration
has not changed. Normal-suite success must be reported separately from this audit.

Findings: modeled-loss allowance across sliced parent orders; after-cost Kelly
sizing; complete projected factor-book identity; finite/nonnegative strategy
exposure. Covered exits must continue to work without entry-only evidence.

The previous production-code repair was tool-blocked. Publishing its reproducer
is not a retry of that repair. No real broker call, market evidence or trade occurs.
These assertions model synthetic acceptance requirements, not guaranteed loss bounds.
