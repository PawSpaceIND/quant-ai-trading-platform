# Integration Checklist

The system can progress through research and deterministic paper simulation without live broker credentials.

Before external connectivity:
1. Choose primary historical + live market-data provider.
2. Choose India broker and US broker independently.
3. Create paper/sandbox credentials first.
4. Store all credentials outside Git.
5. Validate instrument mapping, exchange calendars and symbol aliases.
6. Run data-quality and stale-feed drills.
7. Run broker contract tests for reject, timeout, partial fill and duplicate submission.
8. Complete shadow trading before any real-money order permission.
9. Obtain jurisdiction-specific approval for products offered commercially.

Never make fixed-return or guaranteed-profit claims in the commercial product.
