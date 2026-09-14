# Current-state freshness contract

Pilot readiness must distinguish a current observation from a value that was valid when it was stored. The authenticated workspace ages every current-state claim at read time. Historical research and retained run comparisons remain immutable, dated evidence and are not made current by polling.

## Bounds

| Evidence | Maximum source age | Future tolerance | Failure effect |
| --- | ---: | ---: | --- |
| Engine heartbeat (`updatedAt`) | 10 seconds | 5 seconds | Engine checks become stale and feed coverage is unverified |
| Accepted engine instrument tick | 120 seconds | 0 seconds | The instrument is marked expired, future, missing or rejected |
| Portfolio valuation snapshot | 30 seconds | 5 seconds | Valuation is stale and current marks are withheld |
| Held instrument mark | 120 seconds | 0 seconds | Portfolio becomes degraded and the mark is not current |
| Market collector snapshot | 120 seconds | 5 seconds | Collector quote readiness fails; it cannot refresh engine ticks |
| Strategy manifest / protection evidence | 10 seconds | 5 seconds | The related readiness check fails closed |
| Reconciliation evidence | 120 seconds | 5 seconds | Reconciliation readiness fails closed |

Timestamps must be timezone-aware ISO-8601 values with at most six fractional digits. Impossible dates, naive timestamps, duplicate instrument identities, non-finite values, malformed watchlists and payloads above 1 MB are rejected. A runtime watchlist is bounded to 500 instruments. A stored `fresh` flag never overrides the source timestamp, engine mode, heartbeat or duplicate check.

The UI refreshes every five seconds and advances a monotonic wall-clock estimate every second, including after tab visibility changes. An expired workspace displays a visible status message, removes current readiness and valuation claims, and retains historical research with its original date. Atlas receives the same aged runtime context and the reason for each unverified instrument.

## Engine and collector distinction

An instrument qualifies only when the paper engine is running, the engine accepted the tick, the source timestamp is current and its identity is unique. A collector's `fetchedAt` is a separate observation; a fresh collector row cannot repair an expired or unrecorded engine tick. Markets exposes the engine heartbeat and per-instrument timestamp, age and reason so an operator can tell whether the source is missing, future-dated, expired, rejected or duplicated.

## Verification

The freshness regression covers microsecond boundaries, offset and impossible dates, heartbeat expiry, future and missing ticks, duplicate and malformed watchlists, held-mark expiry, cached workspace expiry, manifest/reconciliation clocks and Atlas/API context. The full local suite passes **716 Python, 99 dashboard and 13 Worker tests**, Ruff, TypeScript and the private Webpack production build. The authenticated production browser smoke was checked at desktop and 390px mobile widths with no document overflow or browser errors; the engine-feed table keeps its own bounded scroll region.

These checks prove fail-closed behavior against synthetic state. They do not prove timestamp authenticity, real exchange-session continuity, reconnect behavior, host-clock correctness, target-host uptime, provider completeness or strategy profitability. Those require the open-session and target-host acceptance drills in the runbook.
