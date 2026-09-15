# Retained observation history

A continuously running paper engine records minute observations outside trading hours as well as inside them. Reading every payload into a comparison capped at 10,000 points could prevent a recent comparison after approximately seven days of uninterrupted telemetry. Counting rows as observation minutes could also allow duplicate timestamp representations to overstate account coverage.

## Windowed comparison

The comparison CLI now selects paper observations in the requested regular-session minute grid. It checks every stored timestamp for an aware, exact minute bucket, resolves equivalent timezone offsets as instants and rejects unlocatable records. Selected payloads keep the existing 10,000-point / 16 MB bounds; a missing expected minute remains a gap. Duplicate selected instants fail validation. No observations are deleted, interpolated or repaired.

The account is still independently reconciled at capture, and its complete recorded fill/fee history remains bounded and included in balance reconstruction. A purchase before the requested window still contributes to starting cash and holdings. Only strategy manifests referenced by selected observations are loaded. Old payloads and unrelated manifests outside the selected grid are not assessed by that comparison. Full-history callers that omit the optional capture window retain the earlier limits.

The source hash includes the selection window, calendar, expected minutes, selected rows, total retained rows and excluded rows. The report, authenticated export, Source and method disclosure and selected Atlas context carry the same counts. The dashboard independently checks the window/count arithmetic. These hashes establish consistency, not market-source authenticity. A captured report remains historical evidence even if the current account subsequently changes.

The maximum requested range remains 45 calendar days and 10,000 expected session minutes. This fix allows a bounded recent comparison on a mature account; it does not promise one report for every minute of an arbitrarily long pilot or remove ledger/replay/report limits.

## Qualification readers

Account performance and strategy-day qualification now stream SQLite rows instead of materializing the entire payload history. Each payload is bounded to 1 MB and must match its timezone-aware stored minute. Day aggregation is limited to 10,000 days. The readers still scan retained history; target-host response time and storage policy require operational qualification.

Both readers require distinct eligible minutes and reject a day with duplicate qualifying UTC buckets, including equivalent offset aliases. They preserve invalid-day evidence discovered after earlier valid observations. Account closes use the latest observation instant, rather than lexical timestamp order, and require consistent finite positive starting capital. Account session/date matching and regular intraday boundaries are checked before counting minutes.

An unparseable payload, inconsistent clock, invalid starting balance or exceeded bound withholds account metrics as `invalid_observations`; Research displays the reason category and the export/Atlas context preserve the status. Strategy qualification fails closed to zero days on unreadable history. The existing minimum of 300 minutes including the closing five minutes is unchanged; it is a coverage filter, not proof of complete feed coverage or profitable strategy performance.

## Verification and limits

The regression fixture contains 43,200 preceding synthetic minute records plus 69 observations in a 70-minute comparison. The selected report retains one invalid minute, one absent minute and one excessive clock difference: 67 pair and aggregate returns stay unavailable. A separate 52,200-record qualification fixture contains 30 eligible synthetic sessions. Offset duplicates, inconsistent clocks and late invalid records cannot manufacture qualified days. Independent pre-window fill reconstruction, capture bounds, current-account reconciliation, changed report rejection and selected Atlas context are tested.

The Linux container drill uses this mature history before and after restoring the selected replay ledger. It also mutates an isolated stored timestamp, checks unavailable current performance through the authenticated API, restores the timestamp and checks recovery. Revision-specific CI artifacts record execution results. No actual provider/model call, order, deployment or operator acceptance occurs. Real sessions, source completeness, intended-host soak/storage/restore, strategy effectiveness and full comparator parity remain open.
