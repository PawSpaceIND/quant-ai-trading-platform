# Receipt-timed observation ingestion

This component receives supplied numeric observations into the existing point-in-time
feature store for shadow research/training. It does not fetch a market feed, fit or
approve a model, schedule a daemon, place orders, or modify the running pilot.

## Why receipt time matters

`observed_at` is the supplied event time. `available_at` is created from this process's
UTC clock when `receive()` starts, after copying its input/grant references. An input
cannot supply `available_at`; that extra field is rejected. Importing old observations
therefore does not make them visible to past decision cutoffs. This is a conservative
local first-receipt timestamp, not independently attested provider availability or an
external trusted clock. The library accepts a test clock; the operator command has no
clock override. A backward move behind the retained last receipt refuses.

A repeated batch retains its original receipt and first-known observation times.
The same source record in a later batch also keeps its first-known time. Reusing a
source record ID for different content refuses. A legitimate later revision needs a
new source record ID; it does not change an earlier point-in-time snapshot.

## Input contract

`pramana.numeric_observation_batch.v1` has exactly four keys: `schema`, `source_id`,
`batch_id`, and `records`. Each record has exactly `record_id`, `subject`, `feature`,
`value`, `observed_at`, `schema_id`, and `category`. Values are bounded finite Decimal
strings, not binary floats. Event times need an explicit timezone. Identity strings
follow the existing feature/dataset identifier contract.

Supply exactly one existing typed SourceGrant for the source. TRAINING permission,
point-in-time semantics, INTERNAL/VERIFIED rights and each record's category are
required. The grant is retained with the receipt. These remain operator declarations,
not authenticated licences, broker ownership, data truth or signature verification.
The batch is copied from immutable bytes, and grants are defensively copied before a
clock callback can run. No token, API key, provider login or `.env` value is required.

Use complete venue/currency/contract subjects and reviewed feature/price/cost schemas.
The ingestion layer deliberately does not invent schema semantics, corporate-action
adjustments or statutory charges. The existing dataset builder still validates its
price and cost bindings, numerical ranges and exact outcome horizon before training.

## Operator command

Prepare a dedicated directory owned by the operator with mode 0700. Input and grant
files must be owned, single-link regular files with no group/other access. The command
uses the existing private-file reader. It rejects invalid batches before initializing
a destination. Do not point it at the live trading ledger or a legacy feature store.

```bash
TRADING_LIVE_MONEY_ACTIVE=false python scripts/ingest_feature_observations.py \
  --input /absolute/private/batch.json \
  --grants /absolute/private/source-grants.json \
  --store /absolute/private/received-features.db \
  --tenant ghost
```

This command ingests a supplied file once. It is NOT a configured provider subscription,
recurring job, source approval, or permission to execute it against production data.
The output says OBSERVATION_ONLY, trading_authorized=false,
source_authenticity_verified=false, and past_availability_reconstructed=false.
It reports only receipt time, insertion count and duplicate status, not private vectors,
source records, grant contents, file paths or arbitrary exception text.

## Atomicity, concurrency and supported recovery

The existing `PointInTimeFeatureStore.append()` now delegates its unchanged insertion
logic to a private helper. Public append behaviour and its checks are preserved. The
new receiver calls that helper inside the same immediate transaction as its source
records and batch receipt. A rejected record or interrupted transaction commits neither
an incomplete batch nor a receipt without its observations. Ordinary callers of the
original feature store are unchanged. Direct append on ReceivedFeatureStore refuses
because there would be no receipt.

A dedicated store carries one local tenant label. An existing unscoped store is not
adopted; no historical receipt or availability time is invented. Same-batch retries are
exact raw-byte/grant retries. Reusing a batch ID with changed bytes or grant contents
refuses rather than replacing prior records. Two connections serialize writers; one
receiver instance also protects its shared connection with a lock. Reopening validates
one read snapshot, and that receiver's snapshot method cannot expose an uncommitted
feature before its receipt commits. Public raw connection use is not a supported bypass.

Receipt tables and original feature history are append-only through the supported SQL
paths. Before/after checks tie receipts to original source records and feature payloads.
Rehashed changed feature values or availability cannot silently match an unchanged
receipt. The selected file's device/inode/privacy is checked, including before reporting
an exact retry. These are local consistency checks, not distributed fencing, external
identity, prevention of coordinated rollback/privileged rewriting, or a guarantee for
every filesystem race. SQLite WAL/SHM coordination can create/update sidecar files.

A process failure during first-time schema initialization can leave a private incomplete
file. Existing incomplete/unscoped files refuse on the next open; inspect rather than
reset or automatically adopt one. No cleanup/deletion command is supplied. A process
failure during ingestion is recoverable by replaying the same batch after inspection.
Successful commit followed by loss of the response also resolves through exact retry.

## Resource bounds and downstream use

Engineering bounds: 1 MB batch bytes, 1,000 records per batch, 64 KiB serialized source
grant, 100,000 unique stored observations, 10,000 receipts, and 32 MB total retained raw
batch/grant bytes. Limits refuse rather than silently rotate/drop evidence. Earlier
receipts are checked before accepting more data. This deliberate replay cost needs a
real host throughput/rotation test before recurring deployment; these are not statistical
sample adequacy, independence or trading-limit claims.

The resulting ordinary feature table is usable by #145's read-only training-package
builder. Late imports remain unavailable to earlier training decisions. The tests also
exercise receipt store -> training package -> numeric fitting -> later shadow forecast
and retry through the actual existing modules, with synthetic values. A validated
receipt or a fitted probability is not proof of useful calibration or trading edge.

## Remaining release gates

Owner review and parent-first merges remain required. No AWS process, user credential,
real source, trading ledger, risk threshold, watchlist, model approval or live-money
setting is changed by this component. The actual provider/feature capture integration,
qualified source/cost semantics, scheduling, cumulative trial governance, unused holdout
and forward evaluation, actual alerts, controlled activation/rollback and real forward
improvement remain separate. Do not relabel the legacy decision journal as receipt-backed.

Evidence records the two initial concurrency regressions and their repairs separately
from production: they were found in this new unpublished implementation. It also retains
the initial new-fixture error (the reused store wraps SQLite insert errors as ValueError),
initial lint findings and the REPL paste error that prevented one intended test run.
The current test counts and complete named guard-removal table are in the PR checkpoint.
