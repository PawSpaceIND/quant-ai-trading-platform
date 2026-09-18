# Building a survivorship-free universe from the exchange archive

The scarce input for honest backtesting is the delisting record. Without it a universe
contains only the companies that still exist, every backtest run on it is biased upward,
and none of them can lose money on a failure. `PointInTimeUniverse.audit` refuses such a
universe outright — which left the pipeline calibrated and empty, because no free symbol
API retains dead names.

The way out is not a vendor. It is the exchange's own daily file.

## Why a bhavcopy is point-in-time by construction

A *bhavcopy* is one CSV per exchange per trading day listing every security that traded on
it. NSE and BSE have published them since the 1990s and **the files are never rewritten**.

A company appears in the file for 14 March 2016 because it traded on 14 March 2016. When
it is delisted it stops appearing in later files, and the earlier ones are unchanged.
Nothing has to *retain* the failures — they were never removed.

This is the exact opposite of a symbol-list API (Yahoo, a broker instrument master), which
answers *what is tradeable now* and so silently deletes every company that died. That is
why `DailyHistoryProvider` (Yahoo) and `kite_history` (Zerodha) are fine for live context
and useless for research: they are survivor-only by design.

## The pipeline

```
fetch_bhavcopy_archive.py   →  bhavcopy.py         →  listing_reconstruction.py
   download, resumable          parse, 3 formats       first/last appearance → Listing
                                                              │
        study_runner.run_universe_study  ←  write_study_inputs┘
                                                              │
                                        action_reconciliation.py
                                          detect splits, size the gap
```

### 1. Download

```bash
python scripts/fetch_bhavcopy_archive.py --exchange NSE \
    --from 2015-01-01 --to 2025-01-01 --out-dir var/bhavcopy/nse
```

Resumable — a day already on disk is never refetched. Rate-limited and sent with
browser-like headers, because both exchanges reject bare scrapers.

A market holiday and a failed download look identical from the outside, so the script
counts them separately and **exits non-zero if anything failed**. Do not build a universe
from an incomplete archive: a run of missing days at the end makes every live security
appear to have stopped trading at once.

The URL patterns do change. If *every* day comes back absent, the patterns in the script
are out of date — not the market closed for a decade. Fix them there.

Run BSE too. BSE carries far more small-cap delistings than NSE, which is precisely the
population a survivor-only dataset is missing.

### 2. Build the universe

```bash
python scripts/build_universe_from_archive.py \
    --archive var/bhavcopy/nse --out-dir var/study-inputs \
    --source "NSE bhavcopy archive 2015-2025"
```

Writes `universe.json` and one replay dataset per instrument — exactly what
`run_universe_study(datasets, manifest, register=...)` reads — plus `archive_report.json`.

Checks before it builds anything: unreadable files abort, and a hole longer than
`--max-session-gap-days` aborts, for the reason above.

### 3. Study

```python
run_universe_study(Path("var/study-inputs/datasets"),
                   Path("var/study-inputs/universe.json"),
                   register=Path("var/trial-register.json"))
```

## What the reconstruction can and cannot tell you

It observes that a security **stopped appearing**. It cannot observe **why**: delisting,
merger, insolvency and a long suspension are indistinguishable in a bhavcopy. So
`delisting_reason` says *"ceased trading; the bhavcopy records the absence, not its
cause"* rather than asserting something the data does not contain.

Three consequences, all reported in `ReconstructionReport.caveats`:

- **The delisting rate is an upper bound.** A suspension counts as a cessation. Since a
  *higher* rate is what the survivorship audit wants to see, this errs in the direction
  that flatters us — so it is stated rather than left to be discovered.
- **Early listings are left-censored.** A security already trading on the archive's first
  session has a real listing date that is earlier and unknown. Those `listed_on` values
  are not IPO dates.
- **Identity is by ISIN, not ticker.** A rename moves the ticker, so symbol keying records
  one company dying and another being born on the same day. `test_without_an_isin_the_same_
  rename_fabricates_a_death_and_a_birth` demonstrates that damage directly. Files with no
  ISIN column fall back to the symbol and are counted, so the exposure is visible.

## Corporate actions: the part that actually bites

Bhavcopy prices are **raw**. A 1:10 split reads as a 90% overnight collapse, a 1:1 bonus
as a halving, and every trend, reversal and volatility feature computed across that
boundary measures an accounting event instead of a price.

**The exchange announces it, in a column nobody reads.** On an ex-date the exchange
restates `PREVCLOSE` on the adjusted basis, so it no longer equals the close it actually
printed the session before. That disagreement *is* the announcement, and the ratio
recovers the factor:

```
close[t-1]   = 1000.00   (what actually printed)
prevclose[t] =  100.00   (restated for a 1:10 split)
implied factor = 10
```

`back_adjust` applies those factors and the series becomes continuous — with **no vendor
data at all**.

This rests on an assumption about venue behaviour rather than a documented guarantee, so a
second, independent detector looks for overnight moves past every circuit limit (20%) with
no such signal. And when real corporate-action records are supplied, `reconcile` reports
whether the `PREVCLOSE` detector actually found them, so the assumption gets validated
against data instead of trusted.

### The number that decides whether to buy anything

`ReconciliationReport.unsignalled_gaps` — breaks the venue never announced. These cannot
be adjusted from the archive and are either an unsignalled action or a data error.

- **Near zero** → the free archive is sufficient. Buy nothing.
- **Large** → buy a corporate-actions table only (Accord Fintech, Capitaline, CMIE
  Prowess), which is far cheaper than a full price licence, and pass it to `reconcile` as
  `CorporateActionRecord` values.

Inspect `worst_unexplained` in `archive_report.json` before paying anyone.

## Series filtering

NSE `EQ` and BSE groups `A`/`B` — normal rolling settlement — are kept by default, because
the friction and impact models elsewhere in this repository assume it. `BE`/`BZ` and `T`/`Z`
are trade-to-trade and surveillance: real equities, different microstructure, wrong cost
model. SME series are thinner than the square-root impact model describes. All are
parseable via `--series` when a study explicitly wants them.

## What is never fabricated

A row that cannot yield a coherent bar — a suspended scrip quoted at `0.00`, a low above
its high, an unparseable number — is **rejected and counted**, never coerced to zero and
never silently dropped. `BhavcopyFile.rejected` carries every one with its reason. A parser
that quietly discards part of its input while reporting success is how a dataset acquires
holes nobody can see.

## Licensing

Public exchange archives are fine for internal research and own-account trading.
Redistributing prices in a product needs an NSE data licence. Worth knowing before, not
after.

## Verification

`tests/test_bhavcopy_archive.py` — 26 tests. Every claim above that is load-bearing was
sabotage-verified: the production code was broken and the named test confirmed red. That
includes the counter-test for ISIN identity, the active-tail rule, the refusal to invent an
adjustment factor, and the refusal to launder a survivor-only archive.

One bug that pass found and is worth recording: stripping an ISO time with `.split("T")`
silently truncated `05-OCT-2019` to `05-OC`, which would have dropped **every October file
in the archive** — about 8% of all data — while reporting success.
