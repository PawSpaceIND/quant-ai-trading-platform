# Live quote timing and ordering

An isolated reproduction on the previous build found that an older arrival replaced the newest price, a future quote passed the cadence reader, a late arrival recreated an already-published minute, and an ignored old tick changed the next bar's cumulative-volume baseline (350 instead of the expected 150).

## Provider time and ingestion

The Zerodha adapter now consumes `exchange_timestamp`. The installed Kite SDK 5.2.1 exposes that field and constructs its naive datetime with host-local `datetime.fromtimestamp(epoch)`. The adapter converts that local value back to UTC instead of relabelling it as UTC. Missing source time is rejected; receipt time is never substituted. Unknown or malformed instrument tokens and malformed packets are counted and skipped without suppressing later valid packets. Sources: [official SDK parsing](https://github.com/zerodha/pykiteconnect/blob/master/kiteconnect/ticker.py), [official packet specification](https://kite.trade/docs/connect/v3/websocket/).

The buffer rejects nonfinite/invalid price, volume or quoted values, crossed positive bid/ask, malformed timestamps, future observations, older per-symbol observations and exact duplicates of the latest accepted update. Rejected updates do not refresh the prior quote or reach bar callbacks. A rejected future quote cannot move the ordering watermark. Distinct updates within the same timestamp remain in arrival order because the provider's second-resolution timestamps do not establish a unique sequence. Producer callbacks run synchronously in acceptance order; callbacks must remain short and avoid waiting for another producer.

Generic internal naive timestamps retain their existing UTC convention. The provider-specific host-local conversion happens before creating an internal tick. The built-in factory aligns buffer/feed clocks to the daemon clock. Cadence and mark readers independently reject future values, even when a custom buffer bypasses ingress validation. The configured maximum age remains a separate condition; a rejected update cannot keep the last good quote fresh indefinitely.

## Immutable closed bars and observed volume

The aggregator rejects older event times before changing close or cumulative-volume state. A clock-closed minute is sealed and cannot be recreated by a later arrival; future ticks cannot replace the forming bar. A new UTC date resets the volume baseline even if the new day's first cumulative value exceeds the prior value. This matches the pilot's daytime NSE cash scope. First observations and decreases in the cumulative counter establish a new baseline without inventing the unobserved increment.

These are bars from accepted quote observations, not reconstructed exchange trade bars. Missing ticks, sequence recovery, same-second ordering, reconnect gaps, volume corrections and completeness remain unqualified. UTC-day volume resets are not an overnight multi-asset exchange-session model. The existing IBKR adapter's receipt-time semantics are not qualified by the Zerodha fix. Source mapping, corporate adjustments, latency and real-market execution remain separate gates.

## Dashboard and Atlas

Each runtime heartbeat contains `pramana.tick_integrity.v1`: accepted count, fixed rejection categories and one bounded last-rejection observation. Counts cover the current process and reset on restart; they are not a durable raw-tick audit. Markets shows **Engine quote integrity**, current/last-recorded runtime status, counts, received/source times and a handoff to Atlas. The persisted Atlas context contains the same engine diagnostics. The market-watch collector is independently sourced; this panel does not certify its values or make any pilot acceptance gate pass.

Malformed inbound ticks are now rejected before the price cache. If no accepted quote exists, the existing valuation fallback is explicitly degraded and does not qualify as a fresh observation; valid independent protective exits remain available. Existing malformed-account/valuation protections remain in force.

## Verification

All **651 Python, 64 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript and the production dashboard build pass. New tests cover timestamp boundaries, bad values, older/duplicate/future updates, cross-thread callback order, same-second valid updates, sealed bars, independent volume arithmetic and host timezone/DST conversion. An offline drill uses the actual installed SDK to parse binary equity and index packets under UTC, Asia/Kolkata and America/New_York; all six preserve the original exchange time and stale-data veto.

A real paper-engine fixture and compiled dashboard reject injected old, duplicate, nonfinite and future ticks while retaining the two-share holding and mark 100. Desktop/mobile diagnostics and Atlas handoff were checked; the mobile document is 390px and the panel 360px, with no browser warnings/errors. The saved conversation includes the diagnostic context and the expected empty-provider-key error. Temporary processes/tabs were closed; no provider request, real trade, target deployment or acceptance was performed.

The Linux verifier adds the six actual-SDK packet checks and a populated stream-rejection phase to both real deployment images. Its revision-specific artifact must pass before claiming image verification. Real session timing/clock synchronization, full source coverage, target-host operations and AI strategy evidence remain open, together with the wider benchmark backlog.
