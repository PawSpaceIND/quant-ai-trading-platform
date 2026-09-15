# What a replay actually tests

A backtest is only worth reading if it exercised the configuration that trades. Until this change it did not. `HistoricalReplayHarness` built its runtime as `SwarmPaperTradingService(broker=..., xai_logger=...)` and accepted the permissive default for everything else, while `build_ghost_runner` passed the founder's position cap, the blocked asset classes, the book-level risk firewall, the founder instructions and a model client. Every tearsheet the project had produced therefore came from a strictly looser engine than the one that runs. A backtest that validates a system nobody runs is worse than no backtest, because it is believed.

Both paths now assemble their runtime through one builder, `quant_ai.agents.traded_runtime.build_traded_runtime`, and `tests/test_backtest_fidelity.py` compares the two configurations field by field and fails when they diverge. The shared builder removes the easy way to drift; the test is what makes the property enforceable, because a caller can always hand the builder different arguments.

## What the replay now runs

Under `FounderDirectives`, whether supplied or defaulted: `max_open_positions`, the blocked asset classes derived from the allowed set, the `BookRiskFirewall` with the operator's sector map (and, when a caller supplies a return history, the correlation and expected-shortfall limits), and the founder's free-text instructions on the Atlas prompt and every proof. `pramana backtest` reads the same `PRAMANA_FOUNDER_DIRECTIVES_*`, `PRAMANA_SECTOR_MAP_*` and `PRAMANA_EVENT_CALENDAR` configuration the daemon boots from, which means a replayed result now depends on that configuration; the run evidence records which limits were armed for the run. Where an operator has supplied an event calendar, a scheduled-event blackout suppresses a replayed entry exactly as it does a live one, and never blocks an exit.

Directives narrow; they never widen. The baseline `RiskPolicy` limits and the warden still have the last word in a replay as they do live.

## Which decision-maker drew the curve

**A replay is run by the deterministic Atlas consensus, not by the LLM that normally decides.** `AtlasInvestmentAgent.decide` is the rule-based path the live runtime falls back to when no model client is configured, the provider is unavailable, the daily budget is spent or a specialist veto puts the decision on a hard hold. The replay runs that path and labels the result as such.

This is a refusal, not a gap in the implementation. Replaying a model over a past window is not a clean backtest: the model's training data may already contain the window's outcome, and no look-ahead assertion in this engine — including `_assert_no_lookahead`, which can only police bars and provider events — can detect that. `HistoricalReplayHarness(..., decision_maker="llm_consensus")` therefore raises rather than producing a number that would be read as evidence of the AI's skill.

So a tearsheet reader can tell which agent produced a curve, every result carries:

| Surface | Field |
| --- | --- |
| `TearSheet.to_json()` | `decision_maker`, `decision_maker_note`, `traded_configuration_differences` |
| `paper_replay_runs.metadata` | `configuration.decisionMaker`, `configuration.decisionMakerNote`, `configuration.tradedRuntime`, `configuration.tradedConfigurationDifferences` |
| `HistoricalReplayResult` | `decision_maker` |

`configuration.tradedRuntime` is the fingerprint of the runtime that actually ran — the same structure the drift test compares against the daemon's — so a reader does not have to take the label's word for it.

## What a replay still cannot reproduce

Each of these is recorded on every run in `tradedConfigurationDifferences` with its reason, and `tests/test_backtest_fidelity.py` fails if a difference exists that is not written down there.

- **Decision-maker.** As above: deterministic consensus in a replay, LLM consensus live.
- **Agent attribution.** The daemon rebuilds specialist conviction weights from the decision journal at boot. Journal rows carry the realised P&L of trades that closed after the replay window and cannot be bounded to outcomes known at a point in time, so restoring them would feed the window its own future. A replay starts every specialist unscored at weight 1.0, which means a replayed decision is weighted differently from the live one even when the two agree.
- **Pilot runtime hooks.** `snapshot_provider`, the full `pre_submit_check` and `strategy_manifest_provider` are installed by `AutonomousTradingDaemon` in pilot mode and read live state — a reconciled ledger, tick freshness, an open session, a bound strategy manifest, a mark that has not drifted during analysis. A historical bar cannot supply any of that. Only the scheduled-event blackout is answerable from a timestamp, and only that one is enforced.
- **Session halt.** The pilot's protection tick latches a durable kill switch on a drawdown or daily-loss breach, and that halt outlives the breach. A replay has no protection tick. The same limits are still enforced order by order by the risk firewall, but a replayed run resumes trading once equity recovers where the live engine would have stayed halted for the session. **A replay can therefore trade more than the live engine would have, never less**, so a replayed drawdown is a floor on the live one, not an estimate of it.

Beyond the runtime, the replay's scheduling, providers and execution model differ from the deployed engine in ways described elsewhere: [execution and protection assumptions](INTRABAR_REPLAY.md), [what a paper/replay comparison does and does not establish](RUN_COMPARISON.md).

## What this change is not

It does not make any replayed number evidence of profitability, and it moved none of the existing numbers: on the current single-instrument synthetic datasets, with all asset classes allowed and an empty journal, the tightened configuration and the old permissive one produce identical fills and identical equity curves. The drift was latent rather than actively distorting those results. It would have bitten the first time an operator blocked an asset class, supplied a sector map, capped positions below the number a multi-instrument dataset could open, or ran a backtest against a ledger with attribution history in it — and nothing would have said so.
