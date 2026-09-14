# Stored protection and final pilot entry checks

The final paper-broker boundary now requires an explicit positive, finite stop for every pilot BUY. The stop must be below both the reference and simulated fill price; any supplied or inherited target must be above both. Scaling cannot loosen a held stop. The normal autonomous pilot still disables position scaling.

Before committing a pilot BUY, the broker checks persisted entry halts, the entire held book's stored protection, internal reconciliation and the configured symbol/asset-class identity. These checks occur inside the fill's SQLite write transaction. Rejection rolls back account initialization, cash, positions, fills, fees, evidence and the retry key. Covered SELLs retain their existing risk-reducing path.

Pilot scope now persists a symbol-to-asset-class map. Existing symbol-only scope records require the normal pilot factory's explicit instrument configuration before new buys. Covered sales from legacy scope remain available. Configuration rejects an existing holding whose asset class differs from the configured instrument. No asset class or protective price is inferred from a legacy record.

## Coverage versus heartbeat

`pramana.protection_coverage.v1` independently reads raw position levels and ledger head in one SQLite snapshot. It records tenant, observation time, position/coverage counts and up to 50 issue details. Missing stops, invalid numeric levels, invalid quantities and a target at/below its stop are failures. Targets remain optional. A held profit stop above original entry is valid; this report does not assume that it is an unbreached stop.

The daemon checks at startup, before new submissions, before protective sweeps and during telemetry publication. Incomplete coverage latches `paper_position_protection_incomplete`; failed protective executions latch `protective_exit_failed`. Both persist across restart. Fixing a level or becoming flat does not automatically release a fault halt. Existing operator review/reset remains required.

Invalid stored threshold strings remain unchanged in SQLite and visible in the coverage report. Position projections exclude unusable values from comparisons/display, preventing one invalid level from suppressing another holding's valid stop or the same holding's valid target. Unknown/nonfinite marks never produce an invented protective fill.

Research's Pilot readiness view shows **Stored position protection** separately from the heartbeat, entry controls and reconciliation. A passing observation requires a current paper runtime, complete counts, matching tenant/ledger and a current engine valuation, with coverage no more than ten seconds old (five seconds of forward clock tolerance). Missing/invalid holdings appear in the detail. Atlas's persisted runtime context contains the same bounded report.

## Verification and limits

The combined local suite passes 585 Python, 61 dashboard and 13 Worker tests, plus Ruff, TypeScript and the production UI build. New cases cover atomic rejection, friction-crossed/inherited targets, stop loosening, exact instrument scope, legacy migration, tenant isolation, corrupt thresholds, valid independent exits, failed-exit retry, durable halts, report bounds and stale/foreign/ledger-mismatched dashboard evidence.

A temporary real paper engine and compiled private dashboard were exercised with synthetic data: complete → missing → invalid protection, process restart, explicit fixture repair and a halt remaining latched. Authenticated API and browser checks show the failed protection row and affected INFY holding alongside the independent heartbeat. No browser warnings/errors were captured. The Linux container verifier also exercises missing protection, a new post-restart halted heartbeat and restoration without automatic entry resumption; revision-specific CI evidence is published separately.

This validates stored levels and software behavior, not real-market exit liquidity, a guaranteed loss ceiling or strategy profitability. Real feed/session observation, target-host recovery/alerts/soak, AI holdout/forward evidence and the broader benchmark backlog remain open. The raw broker API is an internal component; these checks do not replace every Warden sizing/exposure rule or qualify an external brokerage order lifecycle. No live execution, deployment or operator acceptance is enabled by this change.
