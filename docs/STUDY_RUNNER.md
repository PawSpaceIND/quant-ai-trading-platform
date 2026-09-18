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

## Studying a universe rather than one name

`run_universe_study` takes a directory of datasets and a universe manifest, and is the only
arrangement in which a green light is reachable.

```json
{
  "schema": "pramana.universe_manifest.v1",
  "source": "NSE equity listing history, exchange archive 2016-2020",
  "coverage_from": "2016-01-01",
  "coverage_to": "2020-06-01",
  "listings": [
    {"symbol": "INFY", "market": "INDIA", "listed_on": "2016-01-01"},
    {"symbol": "XYZ", "market": "INDIA", "listed_on": "2016-01-01",
     "delisted_on": "2018-06-01", "delisting_reason": "insolvency"}
  ]
}
```

`PointInTimeUniverse` could always express a universe that remembers its failures. Nothing
could build one from anything but a Python literal, so every study in practice ran on
whatever instruments had files on disk — today's survivors. The manifest is the seam through
which real listing history enters, and it is a plain file rather than a vendor client: the
delisting record is the scarce thing, and it can come from an exchange archive, a broker
instrument master or a paid vendor without this repository depending on which.

A manifest must name its source. A universe with no stated provenance cannot be told from one
assembled from memory.

### What it enforces

**Bars after a delisting are dropped and counted.** A name delisted in 2018 contributes its
real history and then stops. The count is reported, because bars priced after a delisting are
a data-integrity problem and a study that silently trades them is the survivorship the
universe exists to stop.

**A dataset outside the manifest is skipped and recorded as skipped**, never studied.
Otherwise survivorship arrives by the back door as a file nobody declared.

**The search is charged per feature and per name.** Five instruments and twenty-four features
is 120 trials, not 24, and every per-name study in the report carries that number. Reporting
the best of fifty names is fifty chances to find something; without this, adding instruments
would manufacture the result that adding features is already prevented from manufacturing.

### The counter-test

`test_the_same_data_without_delisting_records_is_blocked` runs identical bars through two
manifests — one recording the delisting, one not. Same prices, same statistics. The honest
one passes; the flattering one is refused as `survivor_only`.

## What it is not

A feature study over one instrument's daily bars. No position sizing, no cost model, no
capacity estimate, and `promotion_approved` is always false. It never approves promotion or
live trading.
