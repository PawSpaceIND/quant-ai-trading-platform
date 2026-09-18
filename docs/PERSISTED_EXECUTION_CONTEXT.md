# Persisted institutional request and liquidity plan

M02 continuation, stacked on #137 at `edad0e73a44f32bb332f53a08a55aa5dbb2aa071`.
This adds complete persistence for the existing InstitutionalTradeRequest and
ExecutionPlan objects. It does not complete M01's position-linked risk release,
activate the institutional daemon, or enable live execution.

## Stored data

New coordinator preparations retain both objects in a canonical, versioned,
data-only payload alongside the parent, approved risk authority and slices.
The schema includes proposal and supplied provenance, exact instrument metadata,
capital plan and goal bands, portfolio snapshot, edge evidence, strategy inputs,
correlations, weights, factor positions/policy, execution constraints, currency,
base rate, decision time and all supplied volume buckets. The complete resulting
plan preserves algorithm, lot size, source, each slice's observed volume and
participation. No missing source record, model version or market datum is inferred.

The serializer uses only a closed list of existing data contracts. Decimal values,
enums, date/time values, tuple-keyed mappings, booleans and integers retain their
types. Floats are finite and permitted only where the original contract allows
untyped metadata. Unknown types, duplicate keys, omitted/extra fields, NaN, naive
times, wrong numeric types and noncanonical payloads refuse. It never uses pickle,
loads a model, executes source text or imports a payload-selected module.

Bounds: 1,000,000 encoded bytes, 50,000 nodes and depth 32 per complete snapshot;
integers are restricted to the interoperable 53-bit range, with bounded Decimal
representations. Existing contract validation remains active. Limits fail before
programme creation; inspection cannot silently truncate the saved evidence.

## Commit, identity and reconstruction

The journal adds context_version (default 0), context_payload and context_sha256 columns in its
existing schema migration transaction. New context, approved parent, authority
and slices commit together; context fields cannot be updated. Legacy rows remain
version 0 with no fabricated snapshot. Version 1 without its payload refuses.

Creation and journal reads verify the request against the approved request hash,
the complete plan against its stored plan hash, and both against tenant, decision,
parent, strategy and time. Saved slice schedules must match the plan. Volume and
participation are independently checked against the retained request's buckets
and constraints, not just against a recalculated plan checksum.

`programs.load_context(program_id, tenant_id=...)` reconstructs fresh data objects.
`coordinator.restore_runtime_context(program_id, tenant_id=...)` explicitly binds
those saved inputs after restart. It does not claim a slice, consult a provider,
post accounting, clear a halt or send an order. `execute_due` still requires its
normal separate call and all existing policy, shared-risk, fresh portfolio,
liquidity, Warden and recovery checks. Pending or uncertain dispatch stays subject
to existing recovery, not automatic retry. Already executed slices are not replayed.
Caller-owned and returned nested objects are separate from bound runtime inputs.

Offline bundle verification reports verifiedStoredContexts, legacyMissingContexts
and storedRequestAndPlanVerified per programme inventory. Its global runtime and
activation-authority claims remain false: preserved data is not deployment
identity, authenticated operator review or permission to resume. Existing backup,
receipt, OMS, account binding and accounting verification continues to run.

No actual account was migrated, backed up, restored or executed by this work.

## Evidence and boundaries

48 new synthetic cases cover complete round trips, all four existing execution
algorithms, instrument/provenance and typed-map retention, caller isolation,
restart after a TWAP slice, corruption, tenant checks, legacy records, policy drift,
resource bounds, concurrent duplicate preparation, atomic failure and backup.
Two real subprocess-death boundaries check before and after the parent/snapshot
commit. Four final isolated in-memory guard removals reproduce failures in typed-payload
and request-hash binding, saved-liquidity verification and SQL immutability.

The focused combined suite passes 430 cases, including both existing acceptance
files. The initial three cases specified previously absent persistence APIs.
Intermediate failures exposed an earlier error-message check ordering and an old
writer fixture still supplying new metadata. The original error order was kept,
and the fixture now omits context as well as policy metadata to model a complete
historical writer. Its assertion tree and both acceptance files remain unchanged.
Exact final full-suite, commit and CI results are recorded in the PR.

This retains every field of the current request/plan contracts, not external
provider objects, raw source datasets, model weights, credentials or genuine source
rights. Provenance that was never supplied remains absent. Source qualification,
feature/model deployment binding, derived assessment/report inventory and complete
release manifests remain separate. Local hashes do not authenticate a reviewer or
protect against an actor rewriting all state. Historical snapshots are not current
market measurements. No automatic replay, risk-budget release or operator endpoint
is added. M01 remains open; full M02 source/lineage acceptance and M03 production
daemon assembly are not marked complete by this component's synthetic tests.

The typed payload has its own immutable checksum in addition to the existing
request and plan fingerprints. A regression showed the older request fingerprint
normalizes a metadata datetime to the same text as a string. The separate payload
checksum now detects that type change. Altering only an inspection checksum still
cannot bypass independent approved-request/plan/liquidity checks.

An initial complete run exposed the existing requirement to hold execution when
caller-owned inputs change after approval. Defensive copies alone did not preserve
that refusal. The coordinator now retains a separate caller-input witness: caller
mutation cannot change stored data and still requires explicit rebind before use.
The original regression assertion remains unchanged. Final certification follows
both fixes; the earlier failed run and type-fidelity reproduction remain evidence.
