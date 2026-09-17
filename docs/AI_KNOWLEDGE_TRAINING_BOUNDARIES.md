# AI knowledge access and training provenance boundaries

Continuation of draft #126 from `ac3ee5f0b2eb3af56bfeb6ec1b49e73c415e2a56`.
This is an independent knowledge/training change. The previously tool-blocked
edge/coordinator risk repair is not retried, changed, or bypassed by this work.
No permissions, source grants, models, training datasets or live execution are enabled.

## Reproduced input-boundary defects

Python string-enum equality allowed plain-string planes to pass membership checks,
while identity checks then skipped decision freshness and required-category rules.
A plain `UNVERIFIED` rights string also escaped the enum-identity rejection.
Mutable input sets could expand a frozen grant after construction. The controller's
public grant dictionary was writable. Source/reference metadata was rendered into
prompt lines without quoting, allowing line boundaries to be supplied as metadata.

Training validated run IDs, digests and seed only after invoking the trainer. Invalid
requests could therefore execute caller-supplied work before rejection. Dataset row
counts were not strictly integral and caller-owned source lists remained mutable.
A training run retained the dataset name but not the complete declared dataset contract.
The initial 71-case synthetic acceptance run had 65 failures and 6 passes; those are
parameterized acceptance cases, not 65 independent defects.

## Enforcement

Source grants require declared category/plane/rights enum values, an actual boolean
point-in-time flag and a positive integer freshness limit when specified. Their scopes
are defensively copied to frozensets. The controller exposes a read-only grant mapping
and immutable required-category tuple. Selection rejects raw/unknown plane values
before permission, freshness or required-category checks. Proper enum-valued unverified
rights remain available only on explicitly granted research paths, not training/decisions.

Knowledge item identifiers and references reject control/line-separator characters.
Source and reference fields are JSON-quoted in actual knowledge prompt lines, like
content already was. This is structural input containment, not a general proof that
an LLM is immune to prompt injection. Knowledge still enters Atlas as untrusted data.

Training validates the full run request before calling the supplied trainer. Source
lists are defensively copied and dataset row counts must be positive integers. New
run manifests carry `dataset_manifest_sha256` over the dataset ID, UTC cutoff, row
count, ordered source IDs, source snapshot, feature/label schemas and cost/adjustment
policy IDs. The trained artifact retains its separate content hash. A changed dataset
contract under the same dataset name produces a different dataset binding.

Legacy run manifests without this field retain `None` (unknown). No old history is
backfilled from a dataset name. The wrapper does not promote or deploy a model.

## Validation and scope

The new suites are `test_ai_knowledge_boundaries.py` and
`test_training_provenance_boundaries.py`. Existing Atlas knowledge, learning governance
and learning-outcome suites are run alongside them. Tests use synthetic permissions,
observations and artifact bytes; no real training run or paid provider call is invoked.
Exact full-suite, mutation checks and published CI evidence are in the PR discussion.

These contracts validate declarations, not actual provider licensing, raw data content,
point-in-time authenticity, trainer determinism or learned model quality. The training
wrapper still accepts an externally supplied trainer and a declared dataset manifest;
no new data feed, account, filesystem capability or external service is granted.
The router/controller path is hardened; the normal daemon is not switched to a new
knowledge provider, training service or full institutional execution coordinator.

## Blockers not repaired by this patch

The prior institutional edge audit was rerun separately against this candidate source:
15 failures / 2 passes remain. Its file is still uncommitted in the previous isolated
worktree and is not part of the normal collected CI suite. Existing green CI must not
be described as passing that additional acceptance suite.

The four unresolved findings are enforced edge-loss allowances across a sliced parent,
cost-adjusted Kelly economics, complete projected factor-book identity, and finite/
nonnegative strategy-exposure inputs. The previously blocked repair was not repeated
through another tool or hidden inside this change. Risk/coordinator source is unchanged.
Full institutional daemon wiring, migration/rollback, segment admission, settlement,
the 13 Mac deployment failures and external acceptance evidence remain open.

A well-formed manifest or hash is neither verified market truth nor proof of an edge.
Forward after-cost strategy/calibration results and real-session acceptance still matter.
