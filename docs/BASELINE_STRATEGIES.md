# Deterministic baselines

Five price-only rules, and the evaluator that scores them, exist so that a swarm decision has a number to beat. "The AI decided X" is not evidence until a dumber, cheaper, fully deterministic rule has decided the same thing for free and lost. This document says what the five are, what the report columns mean, and what the evaluator refuses to print.

It does not establish that any strategy has an edge, that the swarm beats these floors, or that a result on one window survives on another. It establishes only what the comparison measures and where it abstains.

## What a baseline is allowed to see

A baseline is a pure function of two inputs: the closed daily bars strictly before the bar it is about to trade into, and the fraction of equity it is currently holding. No news, no fundamentals, no macro, no model, no clock. Two runs over the same bars produce the same trades forever, and `test_a_baseline_is_a_pure_function_of_closed_bars_and_its_own_holding` enforces that rather than assuming it.

The position held through bar *N* is decided from bars strictly before *N* and filled at *N*'s **open**. The evaluator raises `LookaheadError` if a baseline is handed a bar at or after the one it is about to trade into; it does not merely avoid doing so.

Every fill is priced by `MarketFrictionModel`, the same statutory schedule the paper ledger charges: brokerage, STT, exchange and SEBI charges, stamp duty, the depository charge on a delivery sell, and GST at 18% on the service base only, plus spread and square-root impact. A baseline that beat the swarm because it traded free would be worse than no baseline, so the gross curve is computed alongside the net one and both are reported. The gap between them is the point.

Buys are trimmed until the account can pay for them, using the execution bar's own open. That is not lookahead: the order was already sized from the last closed bar, and this is the broker refusing to overdraw the account at the moment of the fill, exactly as the paper ledger refuses it.

## The five rules

| `baseline_id` | Rule | Hypothesis it is the floor under |
|---|---|---|
| `baseline.buy_and_hold` | Fully invested from the first closed bar. | none — it is the thing turnover has to justify itself against |
| `baseline.cash` | Hold nothing, ever. | none — the floor; the only way to score below it is to pay friction for nothing |
| `baseline.momentum.v1` | Long while the last close is above the close 20 bars earlier, else flat. | `momentum.v1` |
| `baseline.mean_reversion` | Buy a close 2 population standard deviations below its own 20-bar mean; exit at the mean. | `mean_reversion` |
| `baseline.volatility_target` | Hold the weight whose realised 20-bar volatility matches 15% annualised, capped at 1, rebalanced outside a 10% band. | none — it is a risk control, not a return forecast |

Three details that are deliberate rather than incidental:

- **Mean reversion is asymmetric.** Entry at −2σ, exit at 0σ. Waiting for a symmetric +2σ exit turns a mean-reversion rule into a trend rule and hides which of the two is being measured.
- **The dispersion is the population standard deviation**, the same estimator `sharpe_ratio` uses, so the sigma in the rule and the sigma in the score mean the same thing.
- **Volatility targeting has a rebalance band** because a weight recomputed every bar would rebalance every bar, and the resulting turnover would be a property of the arithmetic rather than of the market. It is reported in the `turnover` column so a reader can see it.

The volatility-target baseline is fully invested on a calm series and will happily ride a quiet market down. What it is a baseline *for* is the claim that a strategy's Sharpe came from choosing when to be exposed rather than from simply being smaller when the market was wild.

## Reading the report

`format_comparison` prints one fixed-width row per baseline:

```
baseline                       net_ret  gross_ret   sharpe   t_stat  sortino   max_dd     hit  turnover  trips   obs
```

- **`net_ret` / `gross_ret`** — total return after and before all friction. `gross_ret > 0 >= net_ret` is the ordinary outcome for a rule that trades, and it is called out by name in the notes rather than left for the reader to spot.
- **`sharpe` and `t_stat` always travel together.** They are one measurement at two scales: `sharpe = t × sqrt(periods/n) × sqrt(n/(n−1))`. An annualised 6.67 built from a mean whose t-statistic is 0.16 is noise wearing a result's clothes. `BaselineReport.__post_init__` raises if a Sharpe is constructed without the t-statistic behind it, so no future renderer can format its way around the pairing.
- **`turnover` and `trips`** are always printed, even when every ratio is withheld, because "beat buy-and-hold gross, lost after costs" is only visible if they are.
- **`obs`** is the number of return observations — one fewer than the number of bars. It is the *n* under the square root in the annualisation and the *n* in the t-statistic's standard error, which is why `test_the_curve_carries_exactly_one_mark_per_bar_and_one_return_per_step` checks the count rather than trusting it.

Below the table, each baseline's `notes` say what the sample could not support:

| Condition | What is withheld |
|---|---|
| fewer than 30 observations | Sharpe and Sortino are not printed at all |
| fewer than 30 observations | the mean-return t-statistic is not printed |
| no closed round trip | expectancy and hit rate are **undefined, not zero** |
| fewer than 20 round trips | expectancy and hit rate are printed but labelled descriptive of this window, not evidence of an edge |
| too few closed trades | the per-trade t-statistic is withheld |
| a position open at the last bar | its profit is marked, not realised, and has not paid exit friction |
| never traded | no friction was charged, and none was earned |

The significance note carried on every report is not boilerplate: the t-statistic tests the mean per-bar return against zero on the observations shown, is **not** corrected for multiple testing, and per-bar returns are serially correlated. It is an upper bound on how much the Sharpe beside it is worth.

## Annualisation, and why daily bars are required

Ratios are annualised against `annualisation_periods` at the sampling interval of the series. A daily bar is one sample per session, so the factor is derived from the venue's own session length rather than hardcoded to 252.

The evaluator **refuses** a series whose bars are not daily rather than quietly scaling one basis to another. Bars must be spaced at least 20 hours apart (weekends and holidays make gaps longer, never shorter, so only the lower bound is checked). Handing the evaluator one-minute bars raises; it does not annualise them against the wrong basis and print the result.

## Running it

The evaluator reads a replay dataset, so the daily history has to exist first:

```bash
python scripts/fetch_historical_bars.py --years 10 --out-dir var/replay-datasets
```

With no `--symbols`, the pilot watchlist is read from `deploy/founder-directives.example.json`, so this tool and the pilot cannot disagree about what is being traded. Prices are Yahoo's raw quote series; `adjclose` is never read, because it back-adjusts for dividends declared after the bar and is therefore a lookahead leak. Chunks are cached and reused so an interrupted run resumes, and a chunk that fails aborts that symbol rather than writing a short series that looks complete.

Then:

```bash
python -m quant_ai.cli baselines --data var/replay-datasets/RELIANCE.json
```

`--start` and `--end` narrow the window. The run registers five candidate evaluations in the trial register for the same reason a replay does: a sweep of windows must not be reportable as one lucky look. The full report is written to `latest-baselines.json` in the proof directory under schema `pramana.baseline_comparison.v1`, including the SHA-256 of the exact bars it scored. That file is a single slot: a second run overwrites it, so keep a copy before scoring the next symbol.

**`--market` is only for a dataset that does not say what it holds.** A file written by `fetch_historical_bars.py` declares its own symbol, market, asset class, exchange and currency, and that declaration is what the run is scored as; the flag can be omitted, and a flag that contradicts the file is refused rather than obeyed. A hand-written fixture or a CSV declares nothing, and there the flag is *required* — it selects the statutory fee schedule and the session length ratios are annualised against, and neither can be guessed from the prices.

## What this does not do

- It does not compare the swarm to these baselines automatically. It produces the floor; reading a swarm result against it is a separate step.
- It does not correct for multiple testing across baselines, windows or symbols.
- It does not model borrow, leverage or shorting. Every baseline is long-only and unlevered by construction (`max_weight` must be in (0, 1]).
- It does not establish that a rule which scored well on a historical window will score well on the next one.
