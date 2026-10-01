# Feed ingestion boundary and bounded halted recovery

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

An optional `FeedRecoveryPolicy` is a source API on `DaemonRunner`, default `None`.
It is NOT enabled by environment or Compose and this change activates nothing.
Explicit opt-in refuses anything outside the monitored PAPER/NSE engine, durable
SQLite risk store, one audited Zerodha stream and unchanged complete subscription
bijection. It monitors only when existing `prices_expected` says a continuous session
is active, retaining the closing-auction/closed-market exclusions and startup grace.
The threshold is120–900 seconds; polls5–30 seconds. Policy is not a risk/freshness cap.

Only silent raw feed, raw frames without tick callbacks, or a missing connection
callback can request at most ONE socket replacement per process. Before requesting
it, an existing durable halt must match the still-engaged in-memory halt exactly.
It preserves the halt/reason and uses the existing give-up/supervisor thread-safe
close/connect path with the identical configured tokens. It never cancels Kite's
active native retry, clears buffers/reservations, resets risk, changes subscriptions,
places an order or automatically resumes trading. The request is not successful
recovery: only new validated prices/protection evidence can establish that later.
A stalled native reactor may not execute its queued close/connect; exhausted or
unsuccessful recovery therefore requires an operator rather than repeated attempts.

Unknown diagnostics, failed/pending consumer futures, failed subscription sends,
unconfirmed halt persistence and closed/auction sessions refuse replacement. An
event-loop-based watchdog cannot repair a completely blocked event loop; raw/native
progress and pending-consumer evidence help localize that boundary, while process
recovery remains an explicitly scoped operator action. Whole-process same-image
restart, protection interruption and subsequent verification are not CLI actions.

Synthetic tests cover silent/no-error behavior, callback/listener failures, pending
futures, receipt-vs-source/buffer distinctions, ignored old callbacks, native retry
preservation, bounded one-shot replacement behind an unchanged durable halt, absent
policy, session exclusions and refusal states. No production cause, live restoration,
successful broker subscription, trading edge or operational activation is inferred.
