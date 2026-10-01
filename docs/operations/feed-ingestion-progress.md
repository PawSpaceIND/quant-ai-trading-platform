# Feed ingestion boundary and bounded operator observation

The observed silent-stream incident is not explained by a connected socket or a
running process. Synthetic reproduction established a narrower source fact: the
existing supervisor waits for connection-error callbacks, so a silent stream with
no callback leaves it waiting while accepted prices age. This does not prove the
real incident was a network, reactor, parser, event-loop or consumer fault.

The Zerodha adapter now observes the SDK's public raw-message callback before tick
parsing, using only receipt time/counters, never payload bytes. See the
[primary SDK implementation](https://github.com/zerodha/pykiteconnect/blob/master/kiteconnect/ticker.py).
Additional counters distinguish tick callbacks, subscription requests/sending,
queued/completed/failed/cancelled ingestion futures, native retries and generation.
Buffer evidence separately records actual accepted write receipt time and source
observation time. None refreshes a market price or grants entry admission.
`sent_unconfirmed` is not broker acknowledgment or proof of continuing delivery.

The existing runtime integrity payload and regular-session feed-minute evidence
include these bounded diagnostics without a schema migration. Counters cover the
process lifetime; receipt timestamps/current pending/failures describe the current
connection generation. Old SDK callbacks are ignored after replacement. Completion
failures are consumed and counted without storing exception text. A listener can
fail after the buffer write: both facts remain visible rather than treating the
future as proof of an unwritten tick. Pending-since is the start of the continuously
nonempty current-generation future group, not the age of a specific oldest task.
No task/payload collection is retained, no provider/credential details are exposed,
and raw frames include SDK heartbeat/text frames, not just usable prices.

An optional `FeedObservationPolicy` is a source API on `DaemonRunner`, default `None`.
It is NOT enabled by environment or Compose and this change activates nothing.
Explicit opt-in refuses anything outside the monitored PAPER/NSE engine, durable
SQLite risk store, one audited Zerodha stream and unchanged complete subscription
bijection. It monitors only when existing `prices_expected` says a continuous session
is active, retaining the closing-auction/closed-market exclusions and startup grace.
The threshold is 120–900 seconds; polls 5–30 seconds. Policy is not a risk/freshness cap.

Only silent raw feed, raw frames without tick callbacks, or a missing connection
callback can produce at most ONE operator alert per process, behind an existing
matching durable/in-memory halt. This observer never queues a connection error,
closes or starts a socket, cancels native retry, clears buffers/reservations, resets
risk, changes subscriptions, places orders or resumes trading. The existing normal
SDK retry and real give-up supervisor paths are unchanged.

Autonomous replacement was deliberately removed before publication. Adversarial
regressions reproduced native retry, halt release/re-engagement (ABA), or generation
changes while journal I/O awaited; the existing replacement path cannot atomically
carry authorization through both supervisor journaling and native thread handoff.
Checking only before/after journal I/O would not close that race. Removing the
replacement request eliminates this authority path rather than treating a delayed
observation as permission to close the current connection.

Unknown diagnostics, failed/pending consumer futures, failed subscription sends,
unconfirmed halt persistence and closed/auction sessions do not generate this
halted-feed alert. An event-loop observer cannot diagnose a completely blocked
event loop while blocked. Native progress/pending-consumer evidence can help after
recovery; same-image process restart, protection interruption and verification
remain separately scoped operator actions. They are not CLI actions in this release.

Synthetic tests cover callback/listener failures, pending futures, receipt-versus-
source/buffer distinctions, ignored old callbacks, native retry preservation, absent
policy, session exclusions and one alert without replacement. Adversarial journal
interleavings cover retry, halt release/ABA, new generation and fresh accepted ticks;
none queues replacement or reaches a native close handoff. No production cause,
live restoration, successful subscription or trading edge is inferred.
