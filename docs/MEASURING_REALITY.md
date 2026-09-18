# Measuring reality against the model

Two checks that close the loop between research and trading. Both default to *no verdict*
when there is not enough evidence, because an unmeasured model is not a correct one.

## Was the cost model right?

`quant_ai.execution.cost_calibration`

Every backtest here is priced by `MarketFrictionModel`. Half of that is exact — brokerage,
STT, stamp duty and GST come from published schedules and there is nothing to calibrate.
The other half, **spread and impact**, is a model, and a model nobody has checked against a
fill is an assumption wearing a number.

This matters more than it sounds. Cost enters a study net of everything, so a model that
undercharges impact by a few basis points promotes strategies whose entire edge *is the
error*. The failure is invisible in the backtest, invisible in the deflated Sharpe, and
shows up only as a live record that doesn't resemble the research.

```python
report = calibrate_costs(fills)
report.verdict                      # insufficient_evidence | calibrated |
                                    # model_understates_cost | model_overstates_cost
report.backtests_are_invalidated    # True only when the model charged too little
report.fills_needed                 # how many trades before a 5bp bias would be visible
```

The test is **paired**: each fill contributes one error, realised minus modelled, so an
instrument's own volatility cancels instead of being mistaken for model bias. Cash charges
are excluded deliberately — they are exact, and including them would dilute the statistic
with a term that cannot be wrong.

Measurement is against the **arrival price**, not the day's close or VWAP. Anything later
folds the market's own movement into the cost and makes a slow fill look free in a rising
market.

Verdicts and what they mean:

| Verdict | Meaning |
|---|---|
| `insufficient_evidence` | Fewer than 30 fills. **This is the current state: zero fills, so the model is unchecked.** |
| `model_understates_cost` | Execution cost more than charged. Every passing backtest was priced too cheaply; any strategy whose edge is smaller than the bias was never real. |
| `calibrated` | Not distinguishable from the model. Says "not caught wrong", never "correct". |
| `model_overstates_cost` | Conservative. Safe to be wrong in this direction, but it hides strategies whose real edge clears the true cost. |

## Has it traded long enough to mean anything?

`quant_ai.validation.track_record`

Forty profitable sessions feel like proof and are not. At a daily Sharpe consistent with a
good strategy, forty observations cannot separate genuine edge from a coin that landed
well.

```python
verdict = evaluate_track_record(daily_returns, backtest_sharpe=0.09)
verdict.sessions_remaining    # how many more before the record could be significant
verdict.supports_going_live   # only for consistent_with_research
```

`backtest_sharpe` is the **per-session** figure the research claimed. Passing an annualised
number against daily returns compares two different quantities and makes every record look
catastrophic.

Verdict precedence is deliberate, and both orderings were bugs I had to fix:

1. **`too_short`** — below 60 sessions nothing is concluded, in either direction.
2. **`no_edge`** — realised Sharpe does not exceed zero. This outranks `degraded`: a
   strategy that doesn't beat zero isn't *underperforming its research*, it isn't working.
3. **`degraded`** — significantly below the backtest. This outranks the remaining length
   test on purpose. Whether a record has established an edge and whether it has fallen
   short of its research are different questions, and the second can be answered sooner.
   Telling someone to run another 470 sessions when their strategy is already measurably
   below its backtest costs them two years.
4. **`too_short`** again — promising but not yet significant; reports the sessions left.
5. **`consistent_with_research`** — the only verdict that supports going live.

Degradation is not automatically failure — live trading pays costs and delays a backtest
can only estimate. What matters is whether the shortfall is larger than sampling noise
explains.

## What neither can tell you

Cost calibration compares realised against modelled spread and impact. It does not identify
*why* a fill was expensive.

The track record measures one record against one backtest claim. It cannot distinguish a
strategy that stopped working from a market that changed, and a record agreeing with its
research is not evidence the research was right about *why* it works.

## Verification

`tests/test_cost_calibration.py`, `tests/test_track_record.py` — 21 tests, deterministic
(no seeded randomness; each verdict is a property of its input). Sabotage-verified: letting
zero fills report `calibrated`, treating an over-charging model as invalidating, dropping
the minimum-fills floor, comparing unpaired means, putting the length test ahead of
degradation, calling a losing strategy merely degraded, and letting any verdict support
going live each turn the named test red.
