# Decision quality: how Gate 2 produces numbers

The paper burn-in answers one question: does the AI's decision process have edge,
and is its confidence honest? This layer turns every cadence decision into a record
with an outcome, so the answer is a number, not an impression.

## What is recorded

Every cadence tick, for every instrument, the daemon writes one row to the
`paper_decision_journal` table in the paper ledger: the stance, confidence, expected
return and risk the proposal carried, the reference price and protective levels, the
market regime label, the evidence mode (`llm`, `llm_unavailable`,
`llm_budget_exhausted`, `unverified_inference` or `deterministic`), what governance
did with it (`filled`, `rejected` with the reason, or `abstained`), and each
specialist agent's stance. Decisions that did not trade are recorded too; a model
that abstains at the right moments is measured on exactly those moments.

Outcomes are attached afterwards, never at decision time: the forward return at
10, 30 and 60 minutes and at the session close, resolved from live marks at the
first cadence after each horizon elapses, and for filled decisions the realised net
P&L after fees, the exit trigger and the holding time once the position closes.
Nothing is estimated; a horizon that cannot be resolved stays null.

The journal is append-only from the daemon's point of view and every write is
wrapped so a failure is logged and the cadence continues.

## The report

After every cadence tick the daemon rewrites `decision-quality.json`
(`PRAMANA_DECISION_QUALITY_REPORT`, default next to the ledger), and
`pramana decision-quality [--since DAYS]` prints the same JSON. The dashboard
renders it under **Decision quality**.

| Number | Meaning | Reads as edge when |
|---|---|---|
| Directional hit rate (60m) | Share of BUY/SELL decisions whose 60-minute forward return had the stated sign | above 0.5 with a sample of at least 20 |
| Expectancy | Mean net P&L per closed paper trade after fees | positive |
| Profit factor | Gross wins divided by gross losses | above 1 |
| Brier score | Mean squared gap between stated confidence and the 0/1 hit | below 0.25 (0.25 is coin-flip calibration) |
| Calibration bins | For each confidence decile, the realised hit rate | hit rate tracks mean confidence |

Breakdowns by regime, hour of day (IST), specialist agent, evidence mode, exit
trigger and rejection reason show where the edge, or the damage, comes from. The
report carries `insufficient_sample: true` until at least 20 directional decisions
have a resolved 60-minute outcome; before that the dashboard says so and no number
should be read as edge.

## Session post-mortems

`pramana post-mortem [--date YYYY-MM-DD]` writes a deterministic session review to
`post-mortems/<date>.json` (`PRAMANA_POST_MORTEM_DIR`): counts, P&L, hit rate,
exits, rejections, the by-regime and by-hour tables, and up to eight one-line
lessons derived only from the numbers, each backed by the decision ids behind it. A
lesson is never drawn from fewer than three observations.

The file starts as `pending`. `pramana post-mortem --approve YYYY-MM-DD` marks it
`approved`. Only approved lessons from the last five sessions, at most eight lines
of at most 200 characters, reach the LLM consensus, and they reach it inside the
evidence block that is labelled as data, not instructions. The dashboard shows both
states; approval is an operator action on the host, never a dashboard button.

## Regime and timeframes

Each proof now carries `regime`: `trending_up`, `trending_down`, `ranging`,
`high_volatility` or `insufficient_history`, computed deterministically from daily
bars when `PRAMANA_DAILY_HISTORY_PROVIDER=yahoo` (default) returns enough history,
else from 15-minute bars aggregated from the session's 1-minute candles. The
specialists receive the regime metrics and the consensus prompt receives the
15-minute and daily bars alongside the 1-minute bars. Sizing and risk gates are
unchanged; regime is evidence and provenance in this release.

## Gate 2 pass criteria

Hold the paper pilot to all of the following over at least 20 sessions before any
discussion of a live adapter:

1. No safety incident: no unprotected position, no duplicate fill, no unexplained
   cadence halt, no proof without a matching ledger row.
2. `insufficient_sample` is false and the directional hit rate is above 0.5.
3. Expectancy after fees is positive and the profit factor is above 1.
4. Brier score below 0.25 and calibration bins that track confidence.
5. Realised drawdown inside the founder directive's limit.
6. The by-regime table shows no regime where the AI loses consistently without
   abstaining there.

If any criterion fails, the outcome is to change the strategy or the abstention
rules and rerun the burn-in, not to go live anyway.

## Operator settings

```
PRAMANA_DAILY_HISTORY_PROVIDER=yahoo      # none: intraday-only regime
PRAMANA_DECISION_QUALITY_REPORT=          # default: decision-quality.json next to the ledger
PRAMANA_POST_MORTEM_DIR=                  # default: post-mortems/ next to the ledger
```

Compose forwards all three; the daemon writes and the dashboard reads the same
files in the `pramana-data` volume.
