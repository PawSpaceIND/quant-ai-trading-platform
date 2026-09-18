# Validation harness

`quant_ai.validation.harness` grades a research candidate against the search that produced
it. It exists because a Sharpe ratio taken from a backtest answers the wrong question.

## The problem it solves

Take 200 strategies built from pure random numbers — zero edge, by construction — and keep
the best one. That is what a parameter sweep does. The winner in this repository's own test
fixture reports:

```
naive Sharpe          0.1263 per observation
annualised            2.00                      <- looks fundable
```

An annualised Sharpe of 2.0 from nothing at all. Every number in that backtest is correct;
the inference drawn from it is not. The harness reports the same candidate as:

```
bar a 200-candidate search sets   0.1379 per observation
deflated Sharpe                   0.399          <- needs >= 0.95
P(backtest overfitting)           0.50           (selection_degrades)
CLEARS GATE                       False
```

## What it runs

| Statistic | Question | Module |
|---|---|---|
| Probabilistic Sharpe | Is the true Sharpe above a benchmark, given sample length, skew and kurtosis? | `deflated_sharpe.py` |
| Deflated Sharpe | Would a search this size have produced this result with no edge? | `deflated_sharpe.py` |
| Minimum track record | Are there enough observations for the claim to be significant at all? | `deflated_sharpe.py` |
| Probability of backtest overfitting | Does picking the in-sample winner generalise, or does it land below the out-of-sample median? | `overfitting.py` |
| Purged, embargoed K-fold | Do overlapping forward labels leak the answer from test into train? | `purged_cv.py` |

## It fails closed on the number of trials

`validate_candidate` requires `trials` and `candidate_sharpes`. There is no default, on
purpose. Defaulting `trials` to 1 would silently restore the exact bias the module exists to
remove, and a sweep of four hundred variants would grade like a single pre-registered
hypothesis. `trials_from_register` reads the cumulative count from the hash-chained trial
register, which already tracked it and already documented that reported statistics were not
corrected for it.

Estimate `candidate_sharpes` from every candidate the sweep evaluated, including the
failures. Dropping the failures shrinks the variance and therefore lowers the bar, which is
the same mistake as not counting the trials.

## The gate that guards the gate

`tests/test_validation_harness.py::test_best_of_a_large_sweep_is_rejected` builds the 200
edgeless strategies described above and asserts the harness refuses the winner. A harness
that passes that candidate is not merely weak — it is one that will eventually put real
money behind noise. Four other tests confirm a genuine edge with a small search still
clears, so the gate is not simply returning `False`.

Every check is sabotage-verified: ignoring the trial count fails 4 tests, defaulting
`trials` to 1 fails 1, removing the degenerate-variance guard fails 1, dropping the label
purge fails 1, and forcing the overfitting rank to "generalised" fails 1.

## The universe the study ran on

The statistics above correct for how hard you searched. They cannot correct for a biased
input: a study run on a universe assembled from the companies that still exist passes every
one of them and is still wrong, because the bias is upstream of the estimator.

`validate_candidate` therefore takes a `universe_audit` from
`quant_ai.marketdata.point_in_time`. A universe that records no delistings at all across
several years is reported as `survivor_only` and blocks the gate; an implausibly low failure
rate is reported as `implausibly_clean`. Omitting the audit is itself a reason, so the gate
is never cleared by a study whose data provenance was never examined.

`test_a_survivor_only_universe_blocks_an_otherwise_perfect_candidate` holds the returns, the
search and every statistic constant and changes only the universe. The clean run clears; the
survivor-only run does not.

## It is wired into promotion, not offered to it

A gate nothing calls is decoration. `PromotionPolicy` now carries
`require_selection_correction`, defaulting to on, and `evaluate_promotion` takes a
keyword-only `SelectionEvidence` carrying the candidate count, the deflated Sharpe and the
universe verdict. Absence is a rejection (`selection_bias_uncorrected`), not a pass.

`governance/pilot_review.py` was already loading the trial register and checking only that
it recorded at least one run — the cumulative candidate count was read and thrown away, so a
strategy chosen as the best of five hundred variants reached promotion on statistics never
corrected for the search that found it. It now builds `SelectionEvidence` from that count
plus the study's typed gate result, and refuses a strategy review that does not carry one.

`learning/candidates.py` threads the same evidence through the hash-chained approval record,
so an approval carries the correction it relied on and a later reader can see the search was
counted rather than assume it.

The escape hatch is `PromotionPolicy(require_selection_correction=False)`, for a genuinely
pre-registered single hypothesis. It has to be stated in the policy, where it is visible in
the signed record, rather than being the default.

## The baseline study corrects itself

`validation/experiment.py` was already the study runner and already registered every run in
the trial register. Its own report listed the gap: "no reported statistic here is corrected
for that multiplicity". The register was counting and nothing read the count back.

`selection_corrected` now deflates the holdout Sharpe against the register's **cumulative**
candidate count, not this run's. That distinction is the point: re-running the study with
another configuration reuses the same holdout, so the tenth run of four configurations has
looked at it forty times, and a statistic corrected only for the four is still wrong.

The same study on a pure random walk, re-run against one holdout:

| cumulative trials | bar the search sets | deflated Sharpe |
| ---: | ---: | ---: |
| 64 | 0.0539 | 0.495 |
| 128 | 0.0628 | 0.461 |
| 192 | 0.0714 | 0.421 |
| 256 | 0.0836 | 0.361 |
| 320 | 0.0961 | 0.305 |

The first run reported a +1.9% holdout return. The bar a 64-candidate search sets is higher
than the Sharpe it achieved, so the gate refuses it — correctly, because no edge exists in
the input by construction.

`candidate_sharpes` keeps every candidate the search evaluated, including the losers.
Dropping them shrinks the measured spread and therefore lowers the bar, which is the same
mistake as not counting the trials.

## One condition, not two

The gate is the deflated Sharpe alone. An earlier draft also required
`observations >= minimum_track_record`, which reads like a second check and is the same
inequality rearranged: PSR ≥ 0.95 at a benchmark is algebraically identical to n ≥ MinTRL at
that benchmark and 95% confidence. Two names for one condition invite a later edit that
changes one and not the other. The track record length is still reported, because "you need
N observations" is actionable in a way a probability is not, and a test asserts the
equivalence so the second check is not added back.

## What clearing it does not mean

Nothing here approves anything. A cleared statistical gate means a result is not obviously
selection noise and the data it ran on is not obviously survivor-only. It says nothing about
capacity, regime coverage or execution realism, and a plausible delisting rate is not proof
that the constituent history is correct. It is not permission to trade.

## References

- Bailey and Lopez de Prado (2014), *The Deflated Sharpe Ratio*.
- Bailey, Borwein, Lopez de Prado and Zhu (2017), *The Probability of Backtest Overfitting*.
- Lopez de Prado (2018), *Advances in Financial Machine Learning*, ch. 7 (purging, embargo).
