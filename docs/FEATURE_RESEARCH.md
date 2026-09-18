# Feature research

`quant_ai.research.feature_study` answers whether a hypothesis predicted anything on
history, with the search counted.

`analytics/` already answers the other question — once a decision has been made and the
future has arrived, was the decision good? That is outcome tracking, and it can only grade
what the system already did. This grades a candidate *before* it is allowed to influence
anything.

## Three properties make the answer honest

**Causality.** A feature at t sees `history[:t+1]` and nothing after. The label is the return
from t to t+horizon, entirely in the future. Two tests hold this: one reconstructs the first
label by hand from raw candles, and one recomputes the study on a truncated series and
asserts the shared rows are byte-identical — later candles cannot change earlier values.

**Purging.** The observation at t and at t+1 share almost all of their label. Ordinary K-fold
leaks that across the boundary and the leak is invisible in the result, so folds are purged
by the label span and embargoed after it. A test asserts the arguments rather than waiting to
notice the symptom.

**The search is counted.** Evaluating N features is N hypotheses, and the deflation charges
for all of them. `test_a_bigger_search_is_charged_for_even_when_the_winner_is_unchanged` runs
the same data through a 1-feature and a 21-feature library where the extra twenty are inert
constants that cannot win a fold: same winner, same returns, and the bar still rises.

## Calibration

A gate that only ever says no is not conservative, it is broken in a way that looks careful.
Sweeping a causally injected mean-reverting force:

| edge strength | OOS Sharpe | deflated | PBO | gate |
| ---: | ---: | ---: | ---: | :--- |
| 0.00 | −0.0891 | 0.000 | 0.73 | refused |
| 0.05 | +0.1166 | 0.037 | 0.21 | refused |
| 0.10 | +0.2621 | 0.855 | 0.03 | refused |
| 0.20 | +0.4291 | 1.000 | 0.00 | **passed** |
| 0.35 | +0.5354 | 1.000 | 0.00 | **passed** |

Selection stability is itself diagnostic and the report carries `fold_winners`: on a real
edge four of five folds choose the same feature; on a random walk the winners scatter.

## Centring and direction

`sign(feature)` only works for a feature centred on zero, and half this library never crosses
it — `month_position`, `range_position`, volatility, turnover. An uncentred signal on those is
a permanent long that uses none of the feature's information and scores well in a drifting
series. Features are centred on a training-set median, and the sign of the relationship is
learned in-sample so a reversal feature is traded as reversal rather than counted as a
failure. Both the centre and the direction come from the training half only.

## Costs come from the engine, not from an assumption

The study prices a round trip through `execution/friction.py` — the same schedules the paper
ledger uses, with GST on brokerage, exchange, SEBI and depository charges but never on STT or
stamp duty. The ATR and average daily volume come from the instrument's own bars, so a thin
name costs more than a liquid one and a small ticket pays a larger fraction than a big one,
because brokerage carries a flat per-order cap. A research loop that prices costs differently
from the engine that would trade them is measuring a strategy nobody can run.

Each fold's test block starts and ends flat, so entering and exiting are both paid for. A
signal that flips every few bars pays the round trip every few bars, and that is usually what
separates an information coefficient from a strategy. `gross_sharpe` is reported alongside the
net figure so the gap is visible; the **net** number is what gets deflated.

The calibration above is net of roughly 48 bps a round trip on a liquid name at a one-lakh
ticket.

## A stronger edge can score worse, and that is not a bug

`expected_maximum_sharpe` estimates the bar from the spread of the candidates actually
evaluated, because the true null is not observable. When many features detect the same real
effect, that spread widens and the bar rises with it — so a broadly-detected edge partly
raises its own hurdle. This is documented behaviour of the deflated Sharpe rather than a
defect here.

The practical consequence is worth stating: **a narrow, pre-registered hypothesis set gets a
result through where a wide search cannot.** Twenty-four features across five names is 120
trials, and at that width a modest edge will not clear however real it is. That is the
correction working as intended, not an argument for turning it off.

## What it is not

Purged cross-validation, not a walk-forward simulation: a fold's training set includes
observations after its test block. That is standard for feature evaluation and the purge and
embargo handle the label overlap, but it is not a claim that a live system could have traded
this path in this order.

It is also not a backtest. There is no position sizing, no cost model and no capacity
estimate, and a good score on survivorship-biased input is still wrong — the bias is in the
input, not the estimator.

Below 250 usable observations the study refuses rather than reporting a number. An earlier
draft required four per fold, which let 51 observations produce a deflated Sharpe and a
probability of backtest overfitting: real statistics computed on a sample too small to carry
them.
