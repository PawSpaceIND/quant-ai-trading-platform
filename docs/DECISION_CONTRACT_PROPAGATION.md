# Decision-time contract propagation and execution recovery

## Implemented scope

The optional immutable Instrument snapshot now survives the actual analysis-request and
Atlas proposal factories, RiskWarden approval, planned child execution, the durable OMS,
and the local paper broker. This is an identity-preservation change, not additional
instrument admission or live-money authority.

`SwarmMarketAnalysisPipeline(bind_order_instruments=True)` selects the bound request path.
The default remains false so an existing cash pilot and its historical evidence are not
silently migrated. Both synchronous and asynchronous request factories use the same binding
helper and the CIO's shared proposal factory. The executable pipeline regression exercises
the synchronous path with synthetic providers, an explicit DurableOms, and a paper fill.

`InstrumentBoundAnalysisRequest` and `InstrumentBoundTradeProposal` defensively freeze the
snapshot and validate symbol/market/asset identity. RiskWarden retains that snapshot in an
InstrumentBoundOrderIntent and checks existing expiry, lot and tick rules before approval.
The planner rejects disagreement between its supplied lot constraint and the contract lot.
Covered rollover exits retain identity and are not blocked by unavailable entry margin;
expired contracts still require reconciliation instead of a fictitious closing trade.

## Durable identities

Bound OMS client IDs incorporate the canonical contract snapshot. The OMS persists the
snapshot in an additive column and in the append-only CREATED event. Verification compares
the projection to that event. Cancel/replacement cannot change contract identity under the
same symbol; a valid repricing replacement starts at CREATED and needs new risk approval.
Legacy unbound client IDs and their historical events remain unchanged. No global registry
is consulted at fill or restart time.

The coordinator persists the exact approved parent order, including side, quantity, price,
protective levels, strategy, tenant, market/class and optional instrument identity. A child
risk approval must equal the originally planned child intent. Restart rebinding must match
the stored parent as well as the existing governance-context digest. A legacy program that
has no parent snapshot refuses rebinding; it is not reconstructed by matching symbol.

Dated contracts can now be serialized into the governance fingerprint. A bound contract's
currency must agree with the requested accounting currency. For the currently supported
cash-equity broker observation scope, the first observation cannot override the snapshot's
exchange or currency. This is NOT a complete broker-account/product/token routing model.

## Migration and rollout limits

The new columns are additive and retained historical values are not inferred or rewritten.
Older binaries that assume positional INSERT column counts are not safe writers of the
new schema. Rollback needs a compatible binary or a tested restore, not an assumed downgrade.
The bound pipeline switch has not been enabled in the user's running daemon or wired into
an operator release/strategy manifest. Default MCX pilot admission remains closed.

The existing expiry guard uses a calendar-day window. This change does not certify exchange
sessions, delivery/tender periods, expiry settlement, daily variation margin, revised entry
margin, derivative fee schedules or a real contract note. Additional broker order types,
time-in-force and routing permissions remain separate requirements. No general claim of
complete crash recovery across all databases is made by the exact-parent recovery test.

## Evidence

Twenty-six new synthetic regression cases, including thirteen that failed before the fix.
The actual pipeline, Warden, planner, OMS, broker and accounting paths are exercised, with
same-symbol changed-contract rejection, restart without duplicate slices, metadata mutation,
legacy missing-parent refusal, and first-observation exchange mismatch tests.

Sabotage checks: removing contract-aware OMS identity failed four tests; removing exact
parent rebinding failed five; dropping the instrument at risk approval failed the bound
end-to-end test. All mutations were restored and the 26 tests then passed.

The complete unmodified-source Mac run recorded 1610 passed, 13 failed and 14 subtests.
All 423 tracked source/test files plus new source/test files in the certification manifest
were hashed before and after the run with no changes. Exact JUnit comparison confirms the
13 failures match the existing deployment-portability failure set. They remain open.
GitHub exact-head Linux CI is the separate cross-platform certification authority; a green
run does not establish qualified real-market data, live readiness, or profitable AI edge.
