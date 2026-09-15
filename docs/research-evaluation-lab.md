# Research evaluation lab

This additive module records comparable research experiments without touching the
running paper engine, its ledger, PR #50's workspace, or broker credentials.
Run with Python 3.12 using `PYTHONPATH=src python -m quant_ai.research.lab --help`.

## What this release implements

- Frozen experiment configuration (candidate identities, baseline, case budget,
  risk/cost assumptions and protocol version).
- Timestamped, hashed input packets with source provenance and availability checks.
- Separate append-only candidate decisions, including explicit provider failures,
  model/prompt versions, source references, usage, USD cost and latency.
- Outcomes accepted only after every registered candidate has a decision or failure.
- Independent long-only NSE equity/ETF round-trip simulation with entry ask/exit bid,
  adverse slippage, proportional costs, cash sizing, liquidity caps and unresolved exits.
- Restart-persistent SQLite comparison reports. No winner, promotion or live orders.

This is a research foundation, NOT a full portfolio backtester or AI training
service. Each case resets to its fixed cash allocation; summed completed-case P&L
is not a portfolio return. Missing/unresolved cases must not be treated as zero
loss. Reports include candidate case outcomes so differing trade coverage remains
visible. The cash baseline must submit HOLD decisions explicitly.

## Executable walkthrough (synthetic; no provider cost)

From the repository root:

```bash
PYTHONPATH=src python scripts/research_lab_demo.py /tmp/pramana-research-demo.sqlite
PYTHONPATH=src python -m quant_ai.research.lab /tmp/pramana-research-demo.sqlite report demo
```

Use a new filename: the demo refuses to reuse an existing database. It does not
load or modify any production/paper account. Labels `synthetic-claude` and
`synthetic-astra` are fixtures, not real model responses or trading evidence.

For integrations, use `ResearchLab.create`, `add_case`, `record_decision`,
`record_outcome`, and `report`. The CLI exposes the same operations using JSON
files (`--file`, `--case-id`, `--candidate`). Get the packet digest from
`sha256(canonical(packet).encode()).hexdigest()`; both candidates must receive the
same canonical packet from the orchestrator. Store secrets outside these packets.

## Integration with the existing build

1. Merge this independent PR normally; it adds new paths only.
2. The integrated pilot branch exposes sanitized reports in its authenticated Research
   screen. See the private workspace publication instructions below.
3. Provider orchestration must freeze the input packet before calling either model,
   record failures without hiding them, and record decisions before future execution
   observations. Reject malformed responses and persist an explicit invalid result.
4. Use separate experiments for each protocol/model/prompt/cost configuration.
5. Add real Claude/Astra adapters in the separate provider-comparison lane. This PR
   makes no API calls and requires no API key. Do not duplicate that lane here.
6. Run forward paper evaluation and independent review before any strategy promotion.

## Explicit limitations and future modules

- Timestamp validation checks supplied metadata. It cannot prove the source is honest,
  the model lacked knowledge of historical outcomes, or an offline importer did not
  select convenient cases. Forward capture and an independently retained audit copy
  are still needed. SQLite append-only API is not tamper-proof against file owners.
- No licensed corporate-action/delisting feed, historical universe service, factor
  model, training pipeline, continuous portfolio, benchmark return series, drawdown,
  options/Greeks/margin support, or risk stress service is added here. Reuse existing
  replay/promotion and PR #50 risk capabilities when integrating rather than copying.
- Execution assumes provided bid/ask and quantities. No queue-position, market-impact,
  exchange-rule, stop-order or tax schedule model; fee_bps is an explicit approximation.
- An exit with insufficient liquidity stays unresolved; realized gains from other
  cases must not be read as total performance.
- API USD cost is reported separately; no fabricated INR exchange conversion.
- No trained weights, automated model changes, winner selection, cloud deployment,
  new alert recipients, broker submission or live-money enablement.

The report deliberately always says `insufficient_evidence`. Defining and validating
an acceptance protocol is a later reviewed change; a high historical P&L alone
must not unlock execution.


## Review exports and execution stress

Read-only commands (never create a missing database):

~~~bash
PYTHONPATH=src python -m quant_ai.research.lab /tmp/pramana-research-demo.sqlite html demo > /tmp/pramana-research-review.html
PYTHONPATH=src python -m quant_ai.research.lab /tmp/pramana-research-demo.sqlite export demo > /tmp/pramana-research-evidence.json
~~~

The HTML view is escaped, script-free and does not include raw source packets or
prompts. It shows comparison blockers, missing evidence and case-level results.
The JSON export includes private input packets, decisions and outcomes; store it
privately. Retain its SHA-256 independently if you need later change detection.
A hash in the same editable file is NOT a digital signature or proof of capture time.

reporting.cost_stress(config, decision, outcome) recomputes each independent
round trip at 1x/2x/3x declared fees and slippage. It recalculates cash-limited
quantity and preserves unresolved exits. This is sensitivity analysis, not
out-of-sample evidence or a complete market-impact model.

The lab refuses databases with unrelated tables, guarding against accidental use
of the running paper ledger. Existing experiment/packet hashes and decision input
links are checked during export and reporting. These detect inconsistency, not
malicious rewriting of every stored hash.


## Verification and integration handoff — 14 September 2026

- Research branch: 313 Python tests pass, including 27 research cases; focused Ruff passes.
- CLI demo, HTML review and private JSON export executed successfully on isolated synthetic data.
- Combined PR #51 source 125ac9a with PR #50 source 3900aa9: clean merge, 411 Python tests pass.
- Initial combined subprocess failure imported the old editable install. The complete rerun
  used an explicit PYTHONPATH pointing to the combined tree; no product workaround was applied.
- These results certify the offline module at those revisions, not future PR #50 changes,
  cloud deployment, actual model calls, trained models or profitable performance.

Integration owner: existing pilot workspace lane. Reuse the authenticated Research
screen and provider provenance conventions. Do not expose private evidence bundles
through the public snapshot viewer. Provider credentials, real side-by-side calls
remain explicitly pending; no real provider verification occurred here. UI/API wiring
was completed by the subsequent integration described below.

## Private Research workspace integration

Publish an explicit experiment from its separate research database:

```bash
PYTHONPATH=src python -m quant_ai.research.workspace_report /private/research.sqlite experiment-id \
  --tenant india-paper --output /private/research-workspace-2026-09-14.json
```

Set `PRAMANA_RESEARCH_LAB_REPORT` on the private Next.js workspace to that absolute
output path, using the same `PRAMANA_TENANT_ID`. Restart the workspace when changing
its environment. Publishing refuses an existing destination, creates mode 0600,
and opens the source SQLite database read-only. Select a new versioned output for
each publication. Merely changing the source database does not refresh a published
snapshot; its publication timestamp remains visible.

The publisher uses one consistent SQLite snapshot and allowlists its output. It
omits input packets, source texts, decision rationales, prompts, and arbitrary
configuration fields. It includes the private evidence snapshot hash, candidate
counts, reported versions/costs/latencies, case outcomes and 1x/2x/3x fee/slippage
sensitivity. Unresolved and missing cases remain explicit. Unsupported stress
assumptions produce unavailable values. The workspace supports at most 16 candidates,
5,000 registered cases and a 2 MB file; oversize reports require a reviewed scaling
change, not silent truncation.

The reader verifies the exact payload digest, tenant, schema, field/count types and
fixed `insufficient_evidence` / no-promotion state. Missing or invalid files produce
an unavailable comparison without breaking the other dashboard panels. These hashes
detect inconsistency; they do not authenticate the publisher or prove capture time.
The report does not satisfy the strategy or deployment acceptance gates.

Research shows candidate coverage and case details, bounded horizontal tables on
mobile, evidence limitations, and an Atlas question action. Atlas receives and stores
the same bounded summary, including its source evidence hash; individual case lists
remain in the dashboard/export. No model is called just by opening a report or
prefilling a question. The separate provider adapters can now generate candidate
decisions; this screen does not initiate those requests. Declared/requested identities
are shown separately from recorded returned identities. Missing API cost is an
explicit count and an incomplete known-cost subtotal, including in Atlas context.
The display does not independently verify provider identities or strategy performance.

`GET /api/research/comparison` downloads the latest validated sanitized report as a
JSON attachment, behind the existing private-workspace session proxy. No user-selected
file path is accepted. This endpoint and report are not available from the hosted
snapshot viewer. The source publisher omits `researchLab` from uploads; the Worker
also removes that field and operator notes before storage.

Integration verification used synthetic loss, unresolved-exit, provider-error,
pending and missing-decision cases. It verified desktop/mobile display, Atlas
prefill, authenticated attachment download, workspace/export equality, rejected
unauthenticated requests, and unchanged readiness gates after a report was damaged.
Provider context/persistence was tested with a mocked transport, not a paid API call.

The combined branch also incorporates #52's [provider adapters, continuous portfolio
journal and company-event evidence](research-provider-portfolios.md). Those modules
are available through their separate CLI/API workflows. Continuous portfolio curves
now have a separate [private Research panel](PORTFOLIO_RESEARCH_WORKSPACE.md).
Company-event mapping controls, real two-provider and live NSE-source qualification
remain open. The original #51
limitations above describe its independent-case lab, not absence of the later simulator.
