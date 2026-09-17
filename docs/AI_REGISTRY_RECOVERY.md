# Explicit AI-registry and training-evidence recovery

Continues draft PR #126 from `94c63230be9daeb5724edd0b29b7544f2e8fbd50`.
This is an offline preservation/verification path, not a model-training, approval,
trading-risk repair or daemon activation path. Existing runtime code is unchanged.

## Selected inventory

The existing recovery-bundle create/restore commands accept an optional `ai_state`:

```json
{"ai_state": {
  "tenant": "REPLACE_WITH_SELECTED_TENANT",
  "registry": "/reviewed/candidates.jsonl",
  "candidates": {
    "candidate-1": {
      "run_manifest": "/reviewed/run.json",
      "dataset_manifest": "/reviewed/dataset.json",
      "artifact": "/reviewed/model.bin"
    }
  }
}}
```

Manifests are JSON representations of the existing TrainingRunManifest and
TrainingDatasetManifest dataclass fields, with aware ISO timestamps. This path
never creates missing metadata, serializes live objects, searches for models,
loads artifact bytes as code or infers a dataset from a reused name.

## What is verified

Schema-5 bundles add a private AI file inventory alongside the existing schema-2/3/4
cash, OMS and institutional components. Missing selection remains `not_selected`;
it is not represented as complete AI coverage. The declared candidate set must
match the full selected registry history, not merely its approved candidates.
File paths are explicitly selected; bundle paths use candidate-ID hashes.

The existing candidate-registry replay verifies chain integrity, legal transitions,
reviewer declarations and recomputed historical approval assessments. Unknown event
kinds, noninteger sequence fields, incomplete records and future evidence refuse.
Each selected run must identify the candidate and dataset, precede its registry
history, and retain the full dataset-contract hash. Artifact bytes must match the
recorded training hash. Missing legacy dataset bindings are not invented.
Restoration recomputes the report and compares canonical JSON, preserving the
distinction between booleans and numbers. Copies never overwrite existing output.

Limits: 128 candidates; registry 16 MiB; each manifest 64 KiB; each opaque artifact
256 MiB; combined selected files 512 MiB. Missing/aliased/nonregular/oversized files
refuse. Original sources remain unchanged; the existing pre/post capture inventory
and private output modes apply. Writers-stopped is still an operator declaration.
Registry locking is local/cooperative, not a distributed or authenticated lock.

## Authority and provenance boundaries

Recorded PAPER_APPROVED stages remain history. Capture/restore never calls a
trainer, deserializes a model, registers/promotes a model, places orders or clears
a halt. A copied model is not an activated model.

The registry does not contain tenant identity or a cryptographic approval-to-model
artifact binding. Tenant and one-run-per-candidate selection are operator declarations,
not authenticated links. Reports explicitly retain artifactApprovalBindingVerified,
datasetBytesVerified, performanceEvidenceVerified, reviewerAuthenticated,
activationAuthorized and liveExecutionAuthorized as false. Dataset/assessment
hashes do not prove source rights, actual dataset contents, genuine market performance
or reviewer authentication. Multiple training runs for one candidate require a
separate reviewed inventory; this selector covers one declared run per candidate.

The in-memory `models.registry.ModelRegistry` has no persisted store to discover;
this change does not export it, persist provider sessions or alter its behavior.
There is no automatic inclusion of unselected AI files or enforcement against a
ledger without an existing AI-state pin. Scheduled multi-store backup, deployment
binding, settlement-state capture, authenticated review and off-host restore remain
separate work. The four institutional risk defects and full daemon integration
remain open; no backup success establishes merge readiness or trading efficacy.

## Test evidence

67 new synthetic tests cover stage/assessment corruption, model/dataset mismatch,
legacy missing metadata, mixed bundle versions, tenant/path declarations, missing
files, size limits, aliases, tampering, failed capture and forbidden training/loading.
The initial three tests failed because the old API did not accept AI selections;
they are capability specifications, not three defects in an existing AI backup.
Two further tests reproduced boolean/numeric report coercion and then passed after
canonical comparison. Four isolated in-memory guard removals reproduced failures.
Focused and full-suite results, exact commits and CI outcomes are recorded in the
PR checkpoint. Only disposable synthetic accounts/artifacts are used in this work.
