# Retained broker observation history

The private Activity workspace can now review an append-only Kite capture journal. It compares retained orders and executions across captures, preserves earlier discrepancies when current records recover, exports a selected capture and scopes Atlas to that capture's preceding evidence. This records observed states; it does not claim every broker event was received or any order was acknowledged by the local platform.

## Collection and source binding

Use the existing GET-only collector with one explicit destination:

```sh
python scripts/capture_broker_observation.py \
  --account-id EXPECTED_KITE_USER_ID --tenant-id india-paper \
  --journal /absolute/private/path/broker-history.sqlite
```

Credentials remain in `KITE_API_KEY` and `KITE_ACCESS_TOKEN`. Each invocation takes the same six reads described in [Broker observations](BROKER_OBSERVATIONS.md), then commits one normalized capture atomically. `--output` remains available for a new single-capture file; it is mutually exclusive with `--journal`. There is no polling scheduler, session renewal or broker write. Review the selected account independently before collecting actual private records.

To retain an existing normalized capture or inspect a sequence offline:

```sh
python -m quant_ai.execution.broker_journal append \
  --database /absolute/private/path/broker-history.sqlite \
  --tenant india-paper --account-ref REVIEWED_64_CHARACTER_REFERENCE \
  --capture /absolute/private/path/broker-capture-001.json
python -m quant_ai.execution.broker_journal inspect \
  --database /absolute/private/path/broker-history.sqlite \
  --tenant india-paper --account-ref REVIEWED_64_CHARACTER_REFERENCE --sequence 1
```

The initial file is mode 0600. Initialization binds a generated journal ID, tenant and account reference. Wrong-account, malformed, future, overlapping and out-of-order captures are rejected without appending. An identical capture hash returns its original sequence, including across concurrent writers to an initialized journal. SQLite WAL transactions serialize appends; update/delete triggers reject routine mutation. A foreign database is rejected before changing its journal mode. A damaged or interrupted initialization is not silently replaced.

Configure `PRAMANA_BROKER_JOURNAL` and `PRAMANA_BROKER_ACCOUNT_REF` on the private dashboard. Leave `PRAMANA_BROKER_OBSERVATION` empty; setting both sources is an error. Container paths must be inside the reviewed private volume. No data migration or automatic discovery occurs.

## Inspection semantics

Python and TypeScript independently replay all retained captures in order. The dashboard obtains the journal metadata, captures and selected result from one read transaction, including while a writer commits another capture.

- A changing capture is retained but does not advance the prior stable baseline.
- An observed order or execution disappearing within the same broker day remains a finding on subsequent empty captures. An empty latest response cannot erase known records.
- Filled quantity regression, changed order/exchange identity, changed prior executions and terminal status/quantity/fill contradictions are flagged. Observed changes to open orders are displayed without assuming undocumented intermediate transitions.
- The next Asia/Kolkata day starts a new daily-book baseline. Previously observed orders without a terminal outcome are counted explicitly; the system does not infer cancellation or expiry.
- Gaps between captures are displayed. Events during those gaps remain unknown.
- Snapshot and lifecycle issue occurrences accumulate through the selected capture. Repeated observations can describe the same incident; these counts are neither unique incident counts nor amounts of loss.

Kite's documented cancelled example retains a nonzero `pending_quantity`. Pending and cancelled quantities can overlap on a terminal order. Checks therefore use `max(pending, filled + cancelled) <= quantity`, and do not require all terminal pending values to be zero. COMPLETE still requires complete quantities and an execution-weighted average within 0.01 quote units. Pending/average cleanup on CANCELLED is shown as a change rather than treated as a new execution or terminal reversal. [Official order fields and examples](https://kite.trade/docs/connect/v3/orders/).

## Dashboard, exports and Atlas

Activity lists the latest 100 captures and supports loading any retained sequence by number. Each selection displays its observation time, gap, snapshot checks, lifecycle findings, bounded change table and existing searchable order/execution detail. Returning to current evidence is explicit. Older than 120 seconds is labelled historical; a missing or changed selected capture has no fallback result.

The authenticated no-store export is `/api/broker/observation?journalId=...&sequence=...&captureSha256=...`. It includes the selected capture and its derived findings, plus current journal metadata and recent history for the operator. The capture hash binds a reviewed selection; it does not authenticate a provider.

An Atlas handoff binds journal ID, sequence and capture hash. The stored model context contains only that capture's time, counts, bounded codes and preceding lifecycle counts. It excludes later findings/history, current portfolio/market data, earlier conversation context and raw account/instrument/order/trade details. Missing evidence produces an error. Company-event and broker-history scopes cannot be combined. Atlas has no broker action tools. The hosted publisher/Worker continues excluding the entire broker observation.

## Retention, recovery and capacity

Select the database explicitly in schema 2 recovery:

```json
"broker-history": {
  "kind": "broker_journal",
  "path": "/absolute/private/path/broker-history.sqlite"
}
```

Add this entry to `research_state`, stop all selected writers, and follow [Recovery bundle](RECOVERY_BUNDLE.md). Capture/restore checks database integrity, all stored rows, the entire capture chain and deterministic lifecycle output. Map `PRAMANA_BROKER_JOURNAL` to the restored `research-state/broker-history` entry. Retain the trusted manifest hash independently; private permissions are not encryption.

Each journal is bounded at 10,000 captures and 128,000,000 payload bytes, whichever comes first. Each capture is at most 8 MB. The reader verifies the full chain, displays at most 100 current findings and 250 changes, and preserves full counts. These are rejection bounds, not a throughput or latency guarantee. Large histories require measured target-host load and storage qualification. There is no automatic truncation or archive rotation; preserve the full journal and a trusted recovery receipt before deliberately selecting another journal. A new journal has a new baseline and does not continue old lifecycle counts. Plan retention before the bound is reached.

The hash chain detects internal holes and body changes. Without an independently retained head/manifest it cannot prove that a whole suffix was not removed, that the entire journal was not rewritten, that a capture was not omitted, or that the source was authentic. SQLite triggers are ordinary write guards, not protection against an administrator replacing the database.

## Verification and remaining gates

Regression tests exercise restarts, concurrent duplicate appends, changing captures, daily boundaries, repeated missing evidence, cancellation semantics, tampering, consistent WAL reads, selected Atlas isolation and schema 2 recovery. A six-capture synthetic production drill spans partial completion, missing execution, two empty captures and recovery to 14 orders/27 executions. Exact browser, build and Linux image evidence is recorded in the revision verification artifacts.

Actual source completeness, capture cadence/session renewal, broker acknowledgements, cancel/replace uncertainty, postbacks/reconnect, cash/position/fee/settlement reconciliation, other order varieties and IBKR executions remain open. [QuantConnect's live/backtest comparison](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation) remains a distinct gap. This increment does not prove profitability, target-host acceptance, live-trading readiness or full platform parity.
