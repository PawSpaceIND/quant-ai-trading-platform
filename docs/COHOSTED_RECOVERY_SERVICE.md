# Recovery listener on the institutional daemon lifecycle

Base: #157 at `4c67b2fd5605df9d17f72a194f817055d94e4a1e`.
This closes explicit in-process service lifecycle assembly. It does not provision
credentials, choose a live account, install a public listener or change deployed defaults.

## One runtime, not a second trading engine

Trusted bootstrap code supplies the already constructed institutional runner,
PersistentApiKeyRegistry and InstitutionalRecoveryOperations. The operations must
reference the exact runtime and kill switch owned by that runner's daemon and the
same tenant. Their selected objects and transport configuration are checked again
before startup. The host does not open or migrate stores or create credentials.

The attachment is explicit and one-shot, before the runner starts:

```python
from quant_ai.operations.recovery_service import RecoveryServiceHost

# runner, keys and operations are existing, explicitly reviewed objects.
# operations.runtime must be runner.daemon.scheduler.pipeline.runtime.
service = RecoveryServiceHost(
    daemon=runner.daemon, keys=keys, operations=operations,
    host="127.0.0.1", port=8765,
)
runner.attach_recovery_service(service)
await runner.start()
```

This example is not an environment-only account launcher. Existing startup without
attachment stays unchanged and opens no recovery socket. The environment-only
build still requires its existing source/runtime/credential provisioning work;
no fabricated edge, liquidity, model or market input is added here.

## Listener and authority

Only literal loopback 127.0.0.1 or ::1 is allowed. Port 0 explicitly requests a
kernel-assigned port, useful for local isolation; service.origin reports the actual
origin only while serving. It is not a public address or account-health certificate.
The listener serves the existing dedicated recovery application and borrows the
existing account objects and locks; there are no trading/model routes or new
reconciliation algorithms. Persistent key and permission checks remain in that app.

The embedded HTTP server does not install process signal handlers. It runs one
worker, without reload, proxy-header trust, access logging or websockets. Socket
backlog, connection and header ceilings bound this local listener, not a claim of
qualified production throughput. Public TLS/gateway and actor-specific identity
remain separate deployment requirements.

## Startup failure does not switch protection off

The existing runner starts independent protection and its selected streams before
starting the optional listener. Listener startup is checked before the analysis
cadence starts. A port conflict, invalid/missing selected credential store, changed
runtime/transport, readiness timeout or unexpected server termination latches an
entry halt. A pre-existing halt and its reason are preserved. There is no automatic
listener restart or halt reset. Bounded failures are logged without credential text.

The runner continues its existing independent protection and operating loop after
a listener failure. The memory halt is set before attempting the daemon's existing
durable halt/notification path. A persistence failure is reported and does not undo
the memory hold. This is not a replacement for independent alert delivery acceptance.

## Orderly shutdown and admitted bookkeeping

For an attached service, request_stop wakes the cadence wait, including when called
from another thread. The original unattached runner stop behavior is unchanged.
The runner stops accepting recovery connections and drains admitted HTTP work before
stopping its independent protection thread or its market streams. Cancellation of
the runner does not cancel the shared drain task. Concurrent stop calls wait on the
same drain. Borrowed broker, accounting, audit, OMS and credential stores remain
caller-owned and open until the host elects to close them after runner termination.

There is deliberately no forced timeout that kills admitted bookkeeping. A stuck
operation can delay shutdown and needs host/operator intervention; protection remains
active while the normal drain waits. Closing a browser or listener is not a rollback.
Durable audit and source idempotency remain the authority for an interrupted outcome.
An exceptional complete process/host failure cannot be made safe by this lifecycle
alone, and existing crash-recovery acceptance remains necessary.

## Verification and remaining scope

New tests use actual loopback sockets and the existing production recovery API with
disposable paper stores. They cover strict configuration and drift, port conflicts,
missing storage, startup deadlines, unexpected listener exit, preserved halts,
continued protection, stop/drain ordering, one-shot attachment, borrowed stores,
process-signal preservation and cadence wake-up. The actual institutional factory
runs a deterministic seeded daemon cycle and an independent protective exit; HTTP
reconciliation then verifies the same account without adding another trade.
No SDK stream or paid-model network call is started by these tests.

The initial failing specification described the previously absent lifecycle API,
not an incident in a real account. Existing tests and trading risk thresholds are
unchanged. Exact frozen-tree and CI evidence is recorded in the pull request.

This does not complete M08 intended-host rollout, TLS/security acceptance or every
M06 operation. It shares one process, not a distributed lease. Synchronous analysis
can delay HTTP processing on the shared event loop; independent protection retains
its own thread. Target-host load, latency, shutdown and storage durability need
real acceptance. Source/model lineage, scheduled execution, coordinated off-host
retention and forward after-cost evidence remain separate requirements.
Position-linked risk changes are in another lane (#158); they are neither changed
nor claimed certified here. No blocked capacity-release operation is retried.


## Test-harness maintenance

The integration run also identified three hardcoded mutation anchors describing the
old startup function. Only those source snippets and their equivalent reordered
mutants were refreshed; all 22 mutation IDs, targeted tests and assertions remain.
Each control must still pass and each deliberately broken guard must still fail.
The complete 195-case focused suite passed after that refresh. Two new minimal
fixtures needed the real broker flush reference, and the signal comparison needed
to capture handlers after asyncio installed its own handler. These were new-test
setup corrections, not changes to any previous assertion or trading safeguard.
