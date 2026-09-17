# Super trading platform closure contract

This is the architecture-level contract for the **full platform**, not the narrower founder-only paper pilot. The system is not called complete because individual features exist or a large test count is green. Every first-class capability must have its own engineering evidence, and every capability that depends on real providers, markets, brokers or operations must separately carry external evidence.

`quant_ai.governance.super_platform` is the machine-readable register. Unknown, empty, `None` or `False` evidence fails closed.

## Design principle

The research plane may be broad; the execution authority stays narrow.

AI may read approved market, fundamental, macro, news, corporate-event, broker-observation, portfolio, risk, cost, historical, decision-journal and operator-approved lesson evidence. Every input must carry source and availability time. AI output is an **opinion** until deterministic portfolio, risk, execution, OMS and account gates approve it.

No LLM, trained model, specialist or online learner can raise a risk limit, bypass an instrument gate, invent missing fees/margin, authorize live money, or convert missing data into confidence.

## AI edge architecture

The intended AI stack is:

1. **Data truth** — point-in-time observations, exact availability timestamps, source/licensing status, adjustment provenance and immutable hashes.
2. **Feature/evidence lineage** — the vector used for a decision is retained with its transformation/version and can be reconstructed without future information.
3. **Knowledge access** — company events, macro, fundamentals, technical state, order-book/liquidity, portfolio/risk state, prior reviewed lessons and research reports are retrievable as bounded evidence rather than prompt instructions.
4. **Specialist swarm** — independent domains publish probabilities/expected return/risk with abstention on missing or stale evidence; an adversarial agent searches for reasons not to trade.
5. **Training registry** — every candidate pins training cutoff, source/data hashes, feature schema, label definition, costs, code/configuration, seed and artifact digest.
6. **Calibration** — trade probability means a frozen, testable event such as `P(after-cost net return > 0 at horizon H)`, not generic model confidence. Brier/reliability/drift are tracked.
7. **Champion/challenger** — new candidates run in shadow first. A candidate cannot replace the traded decision-maker because it looked good in one backtest.
8. **Learning feedback** — realised outcomes may change bounded specialist weights only after minimum samples and regime-aware review. Live self-modifying strategy/risk code is prohibited.
9. **Promotion** — untouched holdout, walk-forward, trial accounting, execution stress and forward paper evidence are bound to the exact model/data/code revision.
10. **Execution separation** — the selected AI proposal still passes capital allocation, portfolio risk, execution planning, OMS, broker/account and protection gates.

The optimization target is **positive after-cost expectancy under an explicit drawdown/tail-risk budget**, not raw win rate or a promise of profit.

## Capability groups

The register covers 33 explicit capabilities across data, research, AI, portfolio construction, risk, execution, accounting, derivatives, operations and security. Important deeper items that existed only partially in the earlier build are now first-class closure gates: durable OMS, strategy portfolio allocation, constrained optimizer, execution planner, double-entry subledger, settlement/collateral, multi-currency cashbook, full options lifecycle, factor/liquidity risk, champion/challenger AI, reproducible training manifests and bounded learning feedback.

## Closure rule

- **Engineering complete**: every required implementation/test/provenance artifact for the capability is present.
- **External complete**: required real-source, broker, target-host, forward-paper or independent-review evidence is present.
- **Launch complete**: both are complete.

No aggregate percentage overrides a red capability. Real-money execution remains a separate release gate.
