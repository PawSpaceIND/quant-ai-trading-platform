# Running the first study on real bars

Everything in the research pipeline has so far been exercised on synthetic series. This is
the sequence that puts real market data through it. It has to run somewhere with outbound
access to the data provider; the development sandbox this was built in denies it by policy.

## 1. Fetch

```bash
python scripts/fetch_historical_bars.py \
    --symbols INFY TCS RELIANCE HDFCBANK ITC \
    --years 8 \
    --out-dir datasets/
```

Writes `datasets/{SYMBOL}.json`, each carrying its own provenance block naming the
instrument, so nothing downstream has to guess what a file holds.

`tests/test_fetch_to_study_integration.py` runs a dataset built by this exact code path
straight into the study, so the two shapes are known to match rather than assumed to.

## 2. Write the universe manifest

This is the part no script can do for you, and it is the part that decides whether the
answer means anything.

```json
{
  "schema": "pramana.universe_manifest.v1",
  "source": "NSE equity listing history, exchange bhavcopy archive 2018-2026",
  "coverage_from": "2018-01-01",
  "coverage_to": "2026-09-01",
  "listings": [
    {"symbol": "INFY", "market": "INDIA", "listed_on": "2018-01-01"},
    {"symbol": "XYZ",  "market": "INDIA", "listed_on": "2018-01-01",
     "delisted_on": "2021-03-12", "delisting_reason": "insolvency"}
  ]
}
```

**A manifest listing only names that exist today will be graded `survivor_only` and the run
will be blocked, whatever the statistics say.** That is not an obstacle to work around — it
is the single most important thing this pipeline does. Yahoo Finance cannot supply delisted
names, so a Yahoo-only universe cannot pass this gate, and a study that appears to pass one
is reporting on a universe with the failures already removed.

The delisting records have to come from somewhere that keeps them: an exchange archive, a
broker instrument master with expiry history, or a paid vendor. That is founder input item 1.

## 3. Study

```bash
python -m quant_ai.research.study_runner \
    --dataset datasets/INFY.json \
    --output reports/infy.json
```

for a single name, or in Python for the whole universe:

```python
from pathlib import Path
from quant_ai.research.study_runner import run_universe_study

report = run_universe_study(
    Path("datasets"), Path("universe.json"),
    register=Path("trial-register.jsonl"),
)
```

## 4. Read the answer honestly

Expect `clears_every_gate: false` on the first run, and probably on the tenth.

A 24-hypothesis search across a handful of names, charged for every feature and every name
and deflated against a register that remembers each re-run, sets a bar that a genuine but
modest edge does not clear on a few years of daily bars. The calibration sweep in
`docs/FEATURE_RESEARCH.md` shows where the line sits.

That is the instrument working. A pipeline that said yes on the first attempt would be one
that had not counted something, and finding that out here costs a report; finding it out
after live capital costs the capital.

Two failure modes to distinguish when it says no:

* `universe is not research-grade` — the data is the problem, not the hypothesis. Nothing
  downstream of it can be trusted, and no statistic will fix it.
* `deflated sharpe ... is below 0.95` — the data was sound and the search did not find
  anything that survives being charged for. Re-running with different settings makes this
  worse, not better, because the register counts that too.
