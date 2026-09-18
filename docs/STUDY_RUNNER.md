# Running the feature study on real bars

`quant_ai.research.study_runner` joins the historical fetcher to the research loop.
`backtesting/history.py` already fetches years of daily bars and writes them in a shape
`backtesting/replay.py` reads back; this runs the study over one of those datasets and
refuses to let the result look better than the data it came from.

```
python -m quant_ai.research.study_runner \
    --dataset datasets/INFY.json --output reports/infy-study.json
```

## Two things it will not let you skip

**The universe is audited, not omitted.** A dataset built from the founder watchlist is
survivor-only by construction: a list of instruments that exist today, chosen by someone who
knows they exist. The audit runs anyway and the verdict goes in the report, because a report
that leaves the universe out reads as one whose universe was fine.

The effect on a real dataset with a genuine edge in it:

```
study.clears_statistical_gate : True     deflated sharpe 0.9998
universe.verdict              : survivor_only
clears_every_gate             : False
blockers                      : universe is not research-grade (survivor_only)
```

Good statistics on bad data are still bad, and `clears_every_gate` says so.

**Trials are registered before the verdict is computed.** A verdict computed first would be
charged for one fewer search than actually happened, and that difference is exactly the bias.
Running the study twice over the same bars puts the cumulative count at 48 rather than 24,
and the deflated Sharpe falls accordingly — looking again is looking again.

## The instrument is read, never guessed

`dataset_instrument` reads the provenance block the fetcher writes. A dataset that declares
nothing is refused rather than defaulted, because guessing produces a report, a trial-register
entry and a proof that all name one security while the prices inside belong to another, with
nothing in the output saying so.

## What it is not

A feature study over one instrument's daily bars. No position sizing, no cost model, no
capacity estimate, and `promotion_approved` is always false. It never approves promotion or
live trading.
