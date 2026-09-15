# Durable protective-exit evidence

Every new stop-loss or take-profit paper fill produced by `ProtectiveExitEngine` now stores a deterministic evidence record in the ledger. The fill, cash/position changes, costs, evidence and optional re-entry cooldown commit in the same SQLite transaction. A failed evidence insert rolls all of them back. This prevents a successful paper liquidation from silently losing its explanation or restart cooldown.

The `paper_protection_evidence` record includes the exact order and tenant IDs, trigger, stored threshold, observed mark, source label and timestamp when available, generated/filled timestamps, quantity, actual simulated fill price and recorded cash fees. The paper broker supplies the reserved fill fields. The source is `custom_resolver_unverified` when a custom resolver does not supply matching observation metadata. Such a record does not establish that the mark was live market data.

The feed-backed resolver records the buffered tick's source/time. Its historical-feed fallback rejects timestamps older than 120 seconds or in the future. When a configured live reader lacks a fresh price, the resolver continues to abstain rather than substitute synthetic history. Stop levels are triggers: gaps and execution friction can produce losses beyond the recorded threshold.

## Dashboard and recovery

Activity resolves these records by tenant and exact order ID, including when no file-proof directory exists. It displays a **Protective exit** label and the deterministic trigger/source explanation in Inspect. It explicitly records that no AI stress vote was involved. These records have no swarm input matrix and never populate the latest AI-swarm intelligence view. Existing fills without evidence stay visibly unlinked; no historic proofs are fabricated.

SQLite backups already include the new table. Cross-file recovery checks order-ID coverage from both file proofs and valid ledger protection records. Its report includes `ledgerProtectionRecords` and `invalidJsonRecords` (which replaces the earlier file-only field name). Coverage is still an ID-presence check, not proof authenticity or strategy validation.

## Verification and remaining acceptance

The full Python suite passes **353 tests**. Added cases cover stop evidence/source/time and cooldown across restart, atomic rollback on an injected evidence-storage failure, take-profit identification, honest custom-source labeling, stale fallback rejection and recovery coverage. All **16 dashboard tests**, TypeScript and the local webpack production build pass.

An isolated synthetic browser drill showed the new stop-loss SELL at 1,389.28, the stored threshold 1,400 and observed mark 1,390, with its custom source correctly labeled unverified. The initial synthetic BUY remained **Missing proof**. The detail view showed no AI stress vote; browser warning/error logs were empty. No actual trade or external alert was sent.

This closes the durable-evidence implementation gap for new protective paper exits. It does not prove open-session feed behavior, actual broker fills, target-host recovery, complete historic proof coverage or AI profitability. A storage fault still prevents a simulated fill; the operational fault/alert procedure must be qualified on the intended deployment. Real-money order handling requires separate broker acknowledgement and reconciliation design.
