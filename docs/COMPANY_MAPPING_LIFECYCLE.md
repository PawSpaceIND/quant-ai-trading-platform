# Company mapping withdrawal and re-review

The private Markets announcement panel can withdraw an incorrect company-to-symbol link without deleting disclosures or prior reviews. Withdrawn mappings stop contributing to current saved-watchlist results and automatic symbol-specific Atlas context. A historical cutoff before the withdrawal still shows the earlier mapping. A later reviewed mapping can restore eligibility from its own recorded time.

## Operator workflow

1. Open the current announcement, expand **Review company-to-symbol mapping**, and choose **Withdraw the current mapping**.
2. Record the reason and reviewed reference, check the withdrawal assertion, then choose **Withdraw mapping**. The server records the time. The confirmation remains visible even when the record disappears from a watchlist filter.
3. Clear **Watchlist only** to inspect the retained disclosure, withdrawn label and mapping history. The original source/revisions remain intact.
4. To restore or correct a link, select **Record a symbol mapping**, review the instrument again and submit a new reference. A fresh server-timestamped row is appended. The original review and withdrawal remain visible.

Historical views are read-only. Withdrawal does not depend on market-list availability; recording a symbol still requires that symbol in the configured NSE market list. Concurrent stale requests fail with 409. Repeated withdrawal, unknown mappings, invalid symbols and missing confirmation/reference fail without another record. A future-dated existing review prevents an earlier server-time write.

The founder session records an operator assertion, not independently certified identity or company facts. Withdrawal changes the company research mapping; it does not cancel orders, rewrite already frozen research cases/conversations, alter positions or approve the strategy. Previously frozen cases containing an incorrect mapping must be reviewed separately and replaced in a new experiment if necessary.

## CLI and storage

Create a private JSON review with `exact_title`, `provenance` and `expected_verified_at` copied exactly from the latest stored mapping. It does not accept a caller-supplied withdrawal time:

```sh
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py event-revoke /data/events.sqlite --file withdrawal-review.json
```

The CLI requires an existing company-event database and performs the same append-only withdrawal with server time and an exact latest-record check. `event-sources` and `event-status` now use read-only connections and cannot initialize a missing store. The existing `event-map` historical-import interface continues accepting an asserted `verified_at`; imported operator timing is not independent evidence of when a review happened.

The original three-table schema remains unchanged. In `symbol_mappings`, an empty `symbol` is the reserved withdrawal marker; it is never a tradable NSE symbol. Exported rows carry explicit `mapped` or `revoked` status. A withdrawal row retains company title, recorded time and reason. Recovery schema 2 preserves and hashes every row, including withdrawals. Historical reads and restores do not resurrect the previous active row.

Both readers compare timestamps to microsecond precision. Different timezone strings representing the same latest instant are treated as conflicting reviews and withheld until a later unambiguous review. The dashboard preserves original timestamp precision in availability and Atlas handoff instead of rounding it to milliseconds. Formatting on screen is local time; exported timestamps retain the precise value.

Legacy Python readers filtering NSE symbols exclude an empty-symbol withdrawal. The earlier dashboard reader rejects such a store instead of silently restoring an old mapping. Use a compatible tested release for rollback; do not remove withdrawal records to make an older reader accept the database. This compatibility behavior does not qualify target-host rollback or off-host recovery.

## Verification — 14 September 2026

- Full regression: **507 Python tests, 43 dashboard tests and 13 Worker tests** pass. CI-scope Ruff, the changed CLI script, TypeScript and the webpack production build pass. Existing Starlette/action-runtime warnings and unrelated script lint findings remain tracked separately.
- The cross-language fixture reproduces byte-for-byte. Python and Node agree on 22 symbol/cutoff snapshots covering withdrawal, remapping, equivalent timezone conflicts, resolution and sub-millisecond revisions. Separate tests verify server-time recording, stale/future/invalid rejection, original-row preservation and explicit manual/automatic Atlas treatment.
- Recovery regression restores a withdrawn mapping with identical history; current sources remain empty and historical sources remain available.
- Browser: a saved-watchlist view changed from two linked announcements to zero after withdrawal, with persistent confirmation. Clearing the filter exposed the withdrawn record and history. A historical view retained the old symbol; a fresh mobile review created a third row and a different synthetic mapping. Mobile document width was 390px, detail 324px and action selector 286px. No warnings/errors were captured in the browser console.
- The browser-submitted Atlas request saved `eventsWithWithdrawnMapping: 2` and zero automatic company events. Its manually selected disclosure carried a null symbol, `mappingState: revoked` and the recorded withdrawal time. An empty provider key produced the expected saved setup error, without a provider request.
- API: unauthenticated mutation 401, missing Origin 403, withdrawal with unavailable market rows 200, stale retry 409, repeat/invalid withdrawal 400, and private export 200. Python read the browser/API history correctly; workspace readiness checks were unchanged.
- Separate CLI withdrawal succeeded, stale retry failed, historical/current source counts were 1/0, and missing stores were not created. The Node reader consumed the Python withdrawal as available evidence with two affected announcement records and no automatic events.

All data in this increment was synthetic. Temporary server/tab sessions were stopped and the viewport reset. No live feed fetch, provider request, order, deployment or real acceptance occurred. Source completeness/rights, independently reviewed instrument mappings, prospective strategy outcomes and target-host operational qualification remain open.
