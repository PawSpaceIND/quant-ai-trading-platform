# Cross-position risk controls

Every limit the engine enforced before this increment was a notional bucket: one
trade, one symbol, one asset class, the gross book, one country. None of them can
tell a diversified book from a concentrated one. Two 4.5% positions in the same
industry are economically one 9% single-factor bet, and every control read them
as two independent 4.5% positions. The covariance mathematics that would have
shown this existed only in the browser, in
`apps/pramana-ui/lib/historical-risk.ts`, labelled a read-only diagnostic, with
no Python module referencing it.

This adds three things: the measure, in Python; three warden limits that use it;
and a stress veto that finally reaches.

Paper only. Nothing here is a forecast, a guaranteed loss limit, a claim that a
loss cannot exceed every observed scenario, or evidence for real-money trading.

## The measure

`quant_ai.risk.portfolio_risk` is a port of the browser module, not a second
implementation of the same idea. The convention is identical in both, so the
dashboard and the engine cannot disagree about the same book;
[the dashboard's contract](HISTORICAL_PORTFOLIO_RISK.md) documents it in full and
is the reference for both.

* `r[i,t] = close[i,t] / close[i,t-1] - 1`, over identical session pairs for
  every instrument.
* Sample covariance of centred returns with an `n-1` denominator.
* Weights are marked position values over total equity, so uninvested cash
  dilutes the book here exactly as it does on the dashboard.
* Portfolio variance `wT S w`, daily volatility its square root.
* Correlation `cov[i][j] / sqrt(cov[i][i] * cov[j][j])`, clamped to `[-1, 1]`,
  and `None` rather than an invented zero when either instrument has no variance
  in the sample.
* Scenario P&L `sum(value[i] * r[i,t])`; signed loss is `-pnl`, so a negative
  loss means the book gained on that day.
* 95% VaR is the nearest-rank 95th percentile of the signed losses. Expected
  shortfall integrates the worst 5%, with fractional weight on the boundary
  observation.
* At least 60 return intervals for covariance, 100 for the tail measures (five
  observations in the 5% tail). Engineering floors, not statistical sufficiency.

Two deliberate differences from the browser. The arithmetic is `Decimal`
throughout, with no float anywhere, so a measure that gates an order is exactly
reproducible from the stored closes. And `measure_book_risk` never raises: every
malformed book, misaligned series, non-positive close, duplicate identity or
short sample comes back as an unavailable measure carrying a reason.

### Where the returns come from

Daily closes, through `quant_ai.risk.book_history.DailyCloseHistory`, which wraps
the `DailyHistoryProvider` the pipeline already uses for regime context. That
provider fetches closed daily bars at most once per instrument per UTC day and
abstains rather than estimating.

The two alternatives were considered and rejected on the facts.
`paper_live_valuations` holds minute-bucketed **book** valuations serialised as
floats — a record of the account, not a per-symbol daily return series, and float
where this must be exact. The tick buffer holds a bounded window of intraday
ticks, nowhere near the 100 sessions the tail measures require. Daily bars are
the only per-symbol return history the engine actually has.

Series are keyed by the venue-local trading date, so an NSE close and a NASDAQ
close on the same day share one session axis. A symbol missing any session inside
the common span withholds the whole book: no pairwise deletion, no forward fill,
no renormalized subset.

## The warden limits

In `quant_ai.risk.policy.BookRiskFirewall`, consulted by `RiskWarden` after the
existing firewall approves an exposure-adding order. All three are additive: they
can turn an approval into a refusal and never the other way round.

### Correlation-adjusted gross exposure

`sqrt(wT R+ w)`, where `R+` is the correlation matrix with every entry floored at
`correlation_floor`, against `max_correlation_adjusted_gross` (default 45% of
equity).

The formula has exactly the property the notional gross cap lacks. Perfectly
correlated positions score `sum(w)` — one bet of their combined size, which is
what they are. Uncorrelated positions score `sqrt(sum(w^2))`, the diversified
equivalent, so real diversification is rewarded. Anything between lands between,
monotonically in the correlations. Two 5% positions count as 10% when they move
together and 7.07% when they do not.

45% is one notch under the 60% notional gross cap, so a perfectly correlated book
is capped tighter than a diversified one while a genuinely diversified book of
`n` equal positions, scoring `gross / sqrt(n)`, can still reach the notional cap.
Concentration is what it refuses, not size.

Flooring negative correlations at zero is deliberate. A negative sample
correlation is the most fragile number a short window produces; letting it shrink
the measure below the independent case would turn an estimation artefact into
permission to add exposure. An undefined pairwise correlation — a constant price
in the sample — is read as perfectly correlated, the conservative direction.

Rejection reason: `correlation_adjusted_gross_limit`.

### Group concentration

No operator-declared group may exceed `max_sector_exposure` (default 25% of
equity). It nests inside the 40% asset-class cap it refines: five positions at
the 5% single-position cap in one industry reach it exactly.

There is no security master in this repository and no defensible way to infer
one, so the mapping is operator-supplied data — a `sector_map` field in founder
directives, `PRAMANA_SECTOR_MAP_JSON`, or `PRAMANA_SECTOR_MAP_FILE`. Symbols the
operator did not map belong to no group and are left to the symbol cap. An
unmapped book is ungrouped, not grouped by a guess, and the limit simply does not
arm when no mapping exists.

Rejection reason: `sector_concentration_limit:<group>`.

### Book expected shortfall

The 95% one-day historical expected shortfall of the projected book — the average
loss across the worst 5% of sampled days — may not exceed
`max_book_expected_shortfall` (default 3% of equity).

The engine's intraday loss breaker is 2% of equity and its drawdown stop 10%.
Requiring a typical tail day to cost no more than 1.5 loss-breaker units, and
under a third of the drawdown budget, leaves room to de-risk before a hard stop
fires rather than discovering the book was too large once it had already
breached.

Rejection reason: `book_expected_shortfall_limit`.

## Arming, and which way it fails

The limits are opt-in by data, not by flag.

* **Unarmed.** No return-history source and no group mapping: the gates do not
  apply and the entry path behaves exactly as it did before. `RiskWarden()` with
  no arguments is unarmed, which is why this increment changes no existing
  behaviour.
* **Armed.** Once an operator supplies a source, an unusable measure is a
  **refusal**, never a pass. Insufficient history, a symbol with no history, a
  missing session close, a malformed series, an arithmetic failure and a raising
  provider all block the entry with
  `book_risk_measure_unavailable:<cause>`, logged at WARNING so an operator can
  see that a measurement rather than the book stopped the trade. An operator who
  arms these controls is asking the engine not to trade blind, so blind means
  stop.

`BookRiskFirewall.evaluate` catches every exception and converts it to a
refusal, so a measurement fault can never raise into the cadence.

Arming is deliberate in deployment too. The group limit arms from the operator's
mapping alone. The correlation and expected-shortfall limits arm only when
`PRAMANA_BOOK_RISK_HISTORY=daily` is set, because a provider outage then stops
new entries; they are not silently attached to the regime-context provider.

**Risk-reducing orders never reach any of this.** `RiskFirewall` returns
`approved_risk_reducing` before any cap is consulted, and `RiskWarden` honours
that verdict before the book gates run, so a covered exit is never trapped by a
measurement problem, a group limit or a correlation estimate. A halt freezes
risk-taking, not exits.

## The stress veto

`quant_ai.intelligence.adversarial.AdversarialStressAgent` shocked the proposed
order and nothing else, vetoing above 1% of equity. A -8% crisis gap on a single
trade only reaches 1% of equity once that trade is 12.5% of equity, and both
`RiskPolicy.max_single_trade_notional` and the position sizer cap a trade at 5%.
The veto was unreachable on the governed path — dead code that made the engine
look stress-tested. It was also the wrong question: a book of five separate 5%
positions gaps together, and no single-trade measure can see that.

Every scenario is now applied to the book the fill would leave: existing
positions at their marked exposure (per asset class where the snapshot carries
that breakdown, the remaining gross at the equity leg), the unwound slice of a
SELL removed, the added slice of the new order included.

Two rules, both of which must hold:

| Rule | Threshold | Flag |
| --- | --- | --- |
| Proposed trade alone | 1% of equity (unchanged) | `STRESS_VETO` |
| Post-fill book | 2% of equity | `BOOK_STRESS_VETO` |

The single-trade rule is preserved verbatim. It is unreachable through the
warden, but the agent is also called directly — `pramana` stress-tests open
positions with it — and relaxing one control while widening another is how a
safety change quietly becomes a loosening.

2% for the book is the engine's own intraday loss breaker
(`RiskPolicy.max_daily_loss`). A crisis gap is precisely the event that breaker
exists for, but the breaker is reactive, firing after the loss, while this is
preventive; tying them together means the engine will not add exposure that one
modelled crisis day would, on its own, turn into a halt. Against the worst
default scenario (-8%) it binds at 25% gross exposure — exactly five positions at
the 5% single-position cap, the largest book the default `max_open_positions` of
five can hold. The veto is therefore reachable well inside the 5% position cap
while the intended book shape is still permitted, and it binds long before the
60% notional gross cap.

A -8% gap across three 4.5% positions is a 1.08% book loss. The default 2%
permits it; an operator who wants it refused sets `book_tolerance_fraction` to
1%. Every verdict reports `book_loss_fraction_of_equity`, and the XAI proof
records it alongside the single-trade figures, so the number to set is visible
before it is set.

`swarm_runtime` still bypasses the veto entirely for covered de-risking SELLs.

## Nothing was loosened

Every change is a refusal added to an existing decision, never a condition
relaxed. Structurally: `RiskFirewall` is unchanged apart from a private helper
becoming a module function; the warden's new gate runs only after the firewall
has already approved; the stress agent's original rule is intact and the new one
is conjunctive.

A differential run over 20,000 randomised order/portfolio combinations compared
this branch against `main` for the firewall verdict, the warden verdict and the
stress verdict. Zero firewall mismatches, zero warden approvals that `main`
refused, zero stress passes that `main` vetoed. The stress monotonicity check is
kept as a test
(`test_the_new_veto_never_permits_what_the_old_one_refused`).

## Known limits

* Every limit here is an intraday limit. All of them, and the stress veto, assume a
  stop that can act, and a stop cannot act on a price that never traded. The gap across
  a session boundary is governed separately, by
  [the overnight gap policy](OVERNIGHT_GAP_POLICY.md), whose gross cap is derived from
  the same −8% scenario and 2% book tolerance used above.
* Beta, factor exposure and a security master are still absent. The group limit
  is a declaration by an operator, not a maintained industry classification.
* The correlation measure is a sample estimate from an unqualified provider's
  adjusted closes. Correlations and tails change; a historical measure does not
  bound a future loss.
* Cross-venue alignment is by local trading date. Instruments whose sessions do
  not overlap in time are still treated as having moved on the same day.
* `validation/monte_carlo.py` and `risk/stress.py::equity_shock` remain without
  callers in `src/`. They were not wired here.
