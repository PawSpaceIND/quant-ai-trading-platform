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
2. PR #50 can later expose `report()` through its existing authenticated workspace.
   No new public route or separate application is required.
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
