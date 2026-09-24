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
| Directional hit rate (60m) | Share of BUY/SELL decisions whose 60-minute forward return had the stated sign | above 0.5, once the promotion gate below passes |
| Expectancy | Mean net P&L per closed paper trade after fees | positive |
| Profit factor | Gross wins divided by gross losses | above 1 |
| Brier score | Mean squared gap between stated confidence and the 0/1 hit | below 0.25 (0.25 is coin-flip calibration) |
| Calibration bins | For each confidence decile, the realised hit rate | hit rate tracks mean confidence |

Breakdowns by regime, hour of day (IST), specialist agent, evidence mode, exit
trigger and rejection reason show where the edge, or the damage, comes from. The
report carries `insufficient_sample: true` until at least 20 directional decisions
have a resolved 60-minute outcome; before that the dashboard says so and no number
should be read as edge.

### Thin samples

Twenty evaluated decisions is enough to print a rate, not to call an edge. Three things
keep the page from overclaiming:

- **The promotion gate** (`promotion`). The verdict `pilot_ops.py calibrate` reaches,
  over this report's window: at least 200 clean resolved forecasts, a Brier score under
  the coin's, positive mean net P&L on filled buys, journaled entry drift, and a realised
  drawdown within the live limit. A basis with a second mapping in it is `basis_mixed`.
  The daemon supplies the book's realised drawdown and its breaker's own limit; if
  either cannot be read, the gate names it missing and refuses. The dashboard reads the
  edge rule only when at least 200 directional decisions are evaluated **and** this
  passes. Anything else shows **Insufficient sample**, never a green edge, with the
  gate's verdict and missing inputs in the sentence.
- **The sample behind each rate.** Each regime, playbook and hour row carries
  `evaluated`, the decisions its hit rate was taken over; most decisions are holds and
  are never evaluated. The post-mortem carries `evaluated_60m`. A rate over fewer than
  `minimum_sample` (20) shows as a dash with its count, and so do the headline hit rate,
  expectancy, profit factor and Brier, and the completed-trade win rate on Research.
- **Why the book held** (`holds`). Each hold is counted once: `hard_hold` (no consensus:
  a veto, stale evidence or missing coverage; never probed), `silent` (no specialist
  leaned), `deadlock` (specialists on both sides), `conviction_floor` (a one-sided lean
  under the entry score or the playbook floor, or held by the model; the journal does
  not say which) and `roster_unrecorded`. The roster shapes are the scorecard's
  (`quant_ai.analytics.scorecards`). Counts only; nothing here reweights or moves a floor.

### Significance

A hit rate above one half and a positive expectancy describe a sample. The
`significance` block asks the first question that should be asked of them: how far
is each mean from zero in units of its own standard error.

| Field | Meaning |
|---|---|
| `forward_return_60m.t_statistic` | One-sample t-statistic of the mean 60-minute forward return against zero |
| `trade_net_pnl.t_statistic` | The same for the mean realised net P&L per closed trade |
| `observations` | The number of observations behind each statistic |
| `minimum_observations` | Below this count no t-statistic is reported at all; the field is null |
| `multiple_testing_correction` | `none` |

Read the correction field literally. Nothing here accounts for how many candidate
strategies, windows or parameter settings were tried before this one was reported;
the trial register beside the ledger is what records that count. Decisions inside a
session are also serially correlated, which inflates a t-statistic further. A large
t-statistic on a swept strategy is not evidence of an edge.

## Session post-mortems

`pramana post-mortem [--date YYYY-MM-DD]` writes a deterministic session review to
`post-mortems/<date>.json` (`PRAMANA_POST_MORTEM_DIR`): counts, P&L, hit rate,
exits, rejections, the by-regime and by-hour tables, and up to eight one-line
lessons derived only from the numbers, each backed by the decision ids behind it. A
lesson is never drawn from fewer than three observations.

The engine builds the file itself once per session: after the venue's close, an hour
after the session's last decision so the 60-minute outcomes had their chance to resolve,
it writes the review `pending` and sends one INFO alert (`POST_MORTEM_PENDING`) with the
lesson count, the path and the approval command. A file that already exists, pending or
approved, is never rebuilt by the engine; `pramana post-mortem [--date YYYY-MM-DD]`
remains the operator's way to rebuild a pending one.

The file starts as `pending`. `pramana post-mortem --approve YYYY-MM-DD` marks it
`approved`. Only approved lessons from the last five sessions, at most eight lines
of at most 200 characters, reach the LLM consensus, and they reach it inside the
evidence block that is labelled as data, not instructions. The dashboard shows both
states; approval is an operator action on the host, never a dashboard button.

## Pre-open session plan

Before each session Atlas writes one plan for the day to `session-plans/<IST date>.json`
(`pramana.session_plan.v1`, `PRAMANA_SESSION_PLAN_DIR`) and sends it as the morning brief
(`SESSION_PLAN_READY`). It is built on the first cadence tick from 08:30 IST on a trading
day (a late boot during regular hours builds it late, marked so; nothing is built after the
close). For every watched name it records the regime read from closed daily bars, the
playbook that regime selects with its floor, size multiplier and probe permission, and any
operator blackout; from those it names the focus list (playbooks that trade the day), the
stand-down list, the blackouts, the names whose regime could not be read yet (the engine
decides them at the plan floor, but the plan does not call them a focus) and a posture for
the book (normal, selective, cautious, defensive, observe). It also quotes the last session's counts and 60-minute hit
rate, its missed moves when the file exists, the probe budget, the lessons in force and the
week's skill weights. The plan comes from the same regime and playbook code the decisions
run on, so it can never promise a stance the engine would not take; it informs the founder
and the record and changes no gate, size or floor. `python scripts/pilot_ops.py plan
--database <ledger> [--date YYYY-MM-DD]` prints it.

With `PRAMANA_SCAN_UNIVERSE_JSON` naming NSE cash symbols, the plan also carries an
**opportunity scan outside the book**: each candidate not already watched is read from
closed daily bars, classified and routed through the same playbooks, and the ones whose
playbook would trade today are listed as opportunities with the gates a promotion has to
pass (a websocket token mapping and a sector group, both operator settings). Nothing is
promoted or ordered by the scan; the watchlist cap and every risk gate apply to a promoted
name exactly as to the rest. Wider venues stay where the operator settings put them: MCX,
NFO, CDS and the other derivative segments are observation-only until contract
qualification and margin sourcing are reviewed, and US names wait on IBKR.

## Specialist skill weights

Realised-P&L credit (below, "What actually learns") needs closed entries, and a book that
holds all day closes none. The weekly skill report scores every specialist on every
decision instead: over the last 10 IST sessions before the current week, how often the
direction it voted matched the 60-minute forward return the outcome resolver stored (a
flat return is a miss; NEUTRAL votes are not scored). A specialist with at least 30 scored
votes gets a weight of `0.75 + accuracy / 2`, clamped to 0.75-1.25, on its stated
confidence; fewer votes keep the weight at 1. The report is `specialist-skill.json`
beside the decision-quality report (`pramana.specialist_skill.v1`), carries a
`basis_sha256` over the decision ids it used, and is recomputed once per week from rows
strictly before the week began, so a restart mid-week reproduces the same weights. With
`PRAMANA_SPECIALIST_REWEIGHTING=on` (the default) the weights apply in the attribution
engine, combined with any realised credit inside the same band, and every weighted
specialist line in a proof carries `skill_weight=…`, `skill_basis_sha256=…` and
`combined_weight=…`. One INFO alert a week lists the weights. Off writes the report and
applies nothing. The weights scale confidence only; they never move a gate, a limit or
the consensus floor.

## Missed opportunities

On 21 September 2026, the first twelve-name session, every decision was a hold, and
nothing above could say what the holds had let go by: hit rates and calibration score
directional calls, and there were none. The missed-opportunity report scores the holds
instead. With `PRAMANA_MISSED_OPPORTUNITY_DIR` set, the daemon rewrites
`<dir>/<IST date>.json` (`pramana.missed_opportunities.v1`) after every cadence tick:
each hold of the session is judged on its 60-minute forward return, `>= +1%` is a
*missed* move a long-only book could have bought, `<= -1%` is an *avoided* one, a null
horizon is *unresolved*. Per symbol it keeps the counts and the largest missed move with
the regime, the evidence mode and the specialists' stances at that decision. After the
close, once per session with at least one hold, one INFO alert summarises the day and
the top five missed lines. `python scripts/pilot_ops.py missed --database <ledger>
--tenant ghost [--date YYYY-MM-DD] [--threshold 0.01]` prints the same report from a
read-only connection.

A missed move is measured on the feed's last traded price at the horizon, ignores costs
and slippage, and is evidence about what the swarm saw, not a claim that the trade
would have been proposed, approved or filled.

## Regime and timeframes

Each proof now carries `regime`: `trending_up`, `trending_down`, `ranging`,
`high_volatility` or `insufficient_history`, computed deterministically from daily
bars when `PRAMANA_DAILY_HISTORY_PROVIDER=yahoo` (default) returns enough history,
else from 15-minute bars aggregated from the session's 1-minute candles. The
specialists receive the regime metrics and the consensus prompt receives the
15-minute and daily bars alongside the 1-minute bars. Sizing and risk gates are
unchanged; regime is evidence and provenance in this release.

## What the page shows, and what it means when it is quiet

The **Decision quality** section carries the verdict banner, the headline numbers, the
calibration strip, the breakdowns and the post-mortems. Two notices sit above them
because a quiet engine has more than one cause:

- **Insufficient sample.** Fewer than 200 directional decisions have a resolved
  60-minute outcome, or the promotion gate has not passed. Rates over at least 20 are
  shown and must not be read as edge; rates over fewer show as a dash with their count.
- **AI budget.** Today's consensus calls and tokens against their caps. When the cap is
  reached, consensus degrades to NEUTRAL and every tick ends in PRESERVE_CAPITAL, so the
  absence of trades after that point says nothing about the strategy. The Atlas chat
  panel shows its own remaining daily allowance for the same reason.

The **Markets** provider tiles report what the engine is configured to use, read from
the environment rather than hardcoded, so the page and the running build cannot drift
apart. They describe configuration, not health: freshness lives in the heartbeat and in
each proof.

## What actually learns

Most of this engine does not learn. The language model's weights are frozen, and the five
specialists apply fixed thresholds; more sessions make their edge *measurable*, not
better. Two things do adapt, and both are bounded and inspectable.

**Attribution.** Every closed trade is attributed to the specialists that argued for the
entry. An agent's realised hit rate scales its confidence on later decisions, between
0.75x and 1.25x. The band is deliberately narrow: no run of luck can hand one agent the
book. Scores are kept per regime as well as blended, because an agent that reads trends
well is often useless in a range; a regime score is only trusted after ten closed trades
in that regime, and until then the blended record governs. Each proof records which
weight applied and where it came from (`attribution_weight=1.08:blended`).

Attribution is rebuilt at every boot from the decision journal, so it survives the daily
restart that the Zerodha token renewal forces. It has no store of its own: the journal is
the record of what each agent said and what the trade earned, so a rebuild is also a
correction, scoring the agents that argued for the entry in the regime the entry was made
in rather than whoever happened to speak when the position closed. An unreadable history
is a cold start, never a failed boot.

**Approved lessons.** The post-mortem loop described above. A session review reaches the
next decision only after you approve it.

Nothing else carries information between sessions. If you want the engine to genuinely
improve rather than merely be measured, these two loops are where that happens, and both
are designed so a bad lesson is bounded and visible rather than silently compounding.

## Gate 2 pass criteria

Hold the paper pilot to all of the following over at least 20 sessions before any
discussion of a live adapter:

1. No safety incident: no unprotected position, no duplicate fill, no unexplained
   cadence halt, no proof without a matching ledger row.
2. The promotion gate passes and the directional hit rate is above 0.5.
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
PRAMANA_MISSED_OPPORTUNITY_DIR=           # unset: off; Compose sets /data/missed-opportunities
PRAMANA_SPECIALIST_REWEIGHTING=on         # weekly skill weights applied; off writes the report only
PRAMANA_SESSION_PLAN_DIR=                 # default: session-plans/ next to the ledger
```

Compose forwards all four; the daemon writes and the dashboard reads the same
files in the `pramana-data` volume.
