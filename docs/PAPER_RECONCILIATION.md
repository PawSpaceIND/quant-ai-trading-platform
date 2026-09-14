# Paper account reconciliation

`PaperBrokerService.reconcile(tenant_id)` reconstructs cash, open quantities, average entry prices and recorded stop/target levels from that tenant's ordered paper fills. Recorded cash-debit fees reduce cash; spread/slippage informational rows are not charged twice. A single SQLite read snapshot is copied under the broker lock, then compared with persisted account and position state. Cash/average-price comparisons allow an absolute 0.00000001 difference for Decimal accumulation rounding.

The report includes checked time, ledger ID, fill/cost counts, expected/observed cash, issue count and up to 50 issue details. It never edits or repairs data. Missing accounts, invalid numbers, unsupported statuses, uncovered sales, malformed fill geometry, orphan/invalid costs and inconsistent positions or protection levels fail the check. This model supports the current immediate-fill paper ledger; live pending/rejected/partial lifecycle events need a different reconciliation model.

## Entry and dashboard integration

Pilot initialization checks the account after explicit account setup. Every governed pilot entry rechecks; a failure engages a persistent `paper_ledger_reconciliation_failed` halt. The broker also rejects direct pilot-scoped buys that fail reconciliation, so a caller cannot bypass the daemon's entry gate. Covered paper sells remain possible; a failed report is not repaired or silently cleared. Repair requires independent investigation and an explicit operator halt reset. Direct broker rejection does not itself write a daemon halt; it rejects the unsafe buy.

Telemetry labels a matched result `outdated` after the ledger advances. The dashboard additionally requires a current running heartbeat and a reconciliation result within 120 seconds before marking this check observed. The check runs at startup and before entries, not on the one-second protection loop. An idle engine's old reconciliation may therefore show as unverified; that is intentional. Full replay cost scales with history and is outside the protection sweep. Qualify this cost on target-host history before expanding scope or throughput.

Read-only operator command (installed repository environment):

```sh
.venv/bin/python scripts/pilot_ops.py reconcile --database /path/to/ledger.sqlite --tenant india-paper
```

Exit 0 means internally matched; exit 2 means a discrepancy. Storage/schema failures also fail the command. Use an actual existing database and exact tenant. The command does not initialize absent accounts, clear halts, submit orders or sign a release.

## Limits

Internal consistency does not prove external broker reconciliation, economic correctness, fee-schedule accuracy, completeness of the event history or absence of coordinated tampering. It does not compare real broker cash, settlement balances, corporate actions, partial fills, cancellations or exchange prices. Those QuantConnect/broker-lifecycle comparison requirements remain open. The starting-capital row and recorded fee events are inputs, not independent evidence.

Verification includes scaled entries, partial/full exits and re-entry; multiple corruption cases without automatic repair; tenant isolation; persisted daemon halt across restart; direct broker enforcement; covered exits; and outdated telemetry after later fills. A read-only CLI check matched the isolated synthetic QA ledger. Browser verification showed matched and synthetic mismatch reports accurately. No real broker balances or live orders were accessed by this change.
