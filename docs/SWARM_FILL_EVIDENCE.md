# Atomic governed paper-fill evidence

The governed swarm paper path previously claimed its duplicate-order key before execution, then wrote its XAI file after the fill. A failure between those steps could consume a retry key without a fill or leave a filled order without its decision evidence.

The runtime now prepares the trace before execution. The broker commits the fill, cash/position updates, fees, canonical decision record and idempotency claim in one SQLite transaction. The new `paper_decision_evidence` table uses schema `pramana.swarm_fill.v1`, distinct from deterministic protective-exit evidence. It contains the declared specialist inputs/confidences, proposal and rationales, risk/stress verdicts, approved order (including strategy ID and protective levels), exact tenant/order IDs, actual simulated fill/fees/time and replay key. These are recorded model outputs, not calibrated probabilities or hidden reasoning.

## Failure and restart behavior

- Trace preparation/serialization failure occurs before any fill mutation.
- Database evidence insertion failure rolls back the fill, cash, positions, fees and replay claim. A retry of the same proposal can succeed once storage recovers.
- A committed fill retains both evidence and replay protection if the process exits before returning or exporting a file. Resubmission of that same key is rejected.
- JSON/Markdown files remain convenient projections. An operating-system error writing them is logged as `xai_file_projection_failed`; the already committed fill is returned with its exact trace. The UI can still read the ledger. Rejected decisions continue to use the existing logger and claim no fill.

Existing consumed keys are not cleared and historical missing proofs are not reconstructed. The legacy direct broker methods and other execution services do not acquire a fabricated AI trace. This change covers `SwarmPaperTradingService` and its governed paper BUY/covered-SELL path. It is not a real-broker exactly-once protocol: remote order acknowledgement and uncertain submission recovery still require a separate implementation.

## Dashboard and recovery

Activity and latest Swarm intelligence read tenant/order-matched ledger evidence even when the file-proof directory is absent. A conflicting file projection cannot override the canonical fill record. Protective exits remain separate and do not become swarm intelligence. Older ledgers without the new tables retain their legacy file lookup behavior.

Cross-file recovery includes the new table through the SQLite backup and reports `ledgerDecisionRecords`. It checks order-ID coverage from canonical swarm/protection records and file proofs. This remains an ID-presence check; it does not authenticate the strategy, prove source quality or qualify its performance.

## Verified evidence

All **359 Python tests** and **17 dashboard tests** pass, with Ruff, TypeScript and a local webpack production build passing. New regressions cover missing file output, an injected database evidence failure, preparation failure, covered SELL evidence, tenant/order validation, conflicting projections and recovery without a file proof. An actual subprocess exits immediately after commit, before the file projection; reopening finds exactly one fill and rejects resubmission of the same key.

The isolated synthetic browser drill displayed the exact BUY order, declared rationale and risk/stress verdicts in Activity, and its `synthetic-technical` input in Overview. The file-projection directory was absent during inspection. Browser warning/error logs were empty. A stopped-writer bundle restored the canonical record with matched accounting and zero missing proof references. An initial capture rejected a source change; the inspection connection was explicitly closed before the successful capture. The drill manifest names the base revision plus separately disclosed local changes, not a qualified deployment release.

No live order, provider inference, external notification, target-host recovery or real release approval was performed. Strategy/version provenance, actual feed observation and AI effectiveness remain independent acceptance requirements.
