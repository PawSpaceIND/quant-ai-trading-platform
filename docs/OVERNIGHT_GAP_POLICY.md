# The overnight gap

Every loss control in this engine assumed a stop that can act.

`CapitalPlan.quantity_for_price` sizes a position so the distance from entry to the
ATR-derived stop costs exactly `per_trade_risk_amount`. `RiskPolicy.max_daily_loss` fires
at 2% of equity on the day. `max_drawdown` stops at 10%. All three are sound because
`ProtectiveExitEngine` sweeps the live tick stream every second and liquidates the moment
a stored threshold is crossed, so the market has to trade through the stop to get past it.

A price that never traded goes past it for free. A position held through a close reopens
wherever the next session decides, and the first mark of that session can be on the far
side of the stop with nothing in between. The stop still fires — it was never suppressed —
but it fires at the open, not at the level it was sized for, and the per-trade risk amount
the sizer solved for describes nothing that happened.

Before this, nothing in `src/` acknowledged that. No end-of-day squaring, no overnight
cap, no gap haircut, no statement of policy. Overnight exposure was not a decision; it was
whatever the last session happened to leave open.

Paper only. Nothing here is a forecast, a guaranteed loss limit, or a claim that a loss
cannot exceed a modelled gap.

## Two halves

The gap creates two separate problems, and they need separate answers.

**Before the close: how much may be carried into a gap at all.**
`quant_ai.risk.overnight.OvernightExposureFirewall`, consulted by `RiskWarden` after the
existing firewall and the book gates have both already approved an exposure-adding order.

**After the open: what the step across the boundary actually was.**
`quant_ai.execution.overnight.OvernightGapMonitor`, told by `ProtectiveExitEngine` what
each sweep priced. It classifies, it alerts, and it escalates. It may re-arm a stop the
re-based-quote guard would have suspended, in the one case where an action is provably
impossible; it can never suspend one, and it never suppresses an exit the stored levels
call for.

## Telling a corporate action from a catastrophic gap

A split, bonus or consolidation cuts the quoted price without changing what the position
is worth, so the stored stop and cost basis stop being comparable with it. A fraud
disclosure, a war or a failed result cuts the quoted price because the position really is
worth less, and that is exactly when the stop has to act.

Both arrive as one step far larger than the exchange band. A size threshold cannot
separate them, and the arithmetic says so plainly: a 1:5 split and a company that loses
four fifths of its value both take the quote to a fifth of where it was. Identical step,
identical ratio, opposite correct response.

`quant_ai.marketdata.gap.classify_gap` returns a verdict and the evidence behind it. Only
one verdict claims the question is settled.

| Verdict | When | Resolved |
| --- | --- | --- |
| `ORDINARY` | Step inside the band (20%) | Yes |
| `DECLARED_ACTION` | The operator's calendar names an ex-date for this symbol today | Yes |
| `INTRASESSION_BREAK` | Step over the band with no session boundary between the marks | No |
| `UNDETERMINED` | Step over the band across a session boundary, undeclared | No |

Three facts do real work here, and one does not.

**A declared ex-date settles it.** It is the only evidence backed by a declaration rather
than an inference, and it is the only thing that resolves a discontinuity outright. The
source is a callable — `(symbol, moment) -> kind or None` — so an operator's own calendar
attaches without this module owning a file format.

**A session boundary rules an action out.** A corporate action re-bases at an open. Two
marks inside one session cannot straddle one, so a step over the band between them was
not an action. That does not say what it *was* — a crash, a bad print and a fat finger are
all still open — but it removes one possibility honestly, which is more than size alone
can do.

**Direction does not discriminate.** Splits and bonuses cut the price; consolidations
raise it; crashes cut it; a short squeeze raises it. Both directions hold both kinds.

**Roundness is reported and never believed.** This is the tempting one and it does not
work. A corporate action re-bases by an exact rational ratio, so a round ratio looks like
proof. But the first price that trades after an ex-date is the theoretical price *plus
that day's real move*, so a genuine 1:5 split routinely prints 0.19 or 0.21, not 0.20.
Widening the tolerance enough to catch those makes the candidate ratios tile the line —
every ratio between roughly 0.1 and 0.96 lands within one exchange band of some small
integer ratio — at which point roundness has stopped discriminating and is only laundering
a guess.

So the tolerance stays tight (0.5%) and the match travels with the verdict as
`nearest_action`, evidence for the operator reading the alert. Even the candidate set is
constrained: one term must be five or below, because a real action always has one small
side ("one new share for every five held", "five for one") and nobody declares eleven for
every seven. Admitting every coprime pair up to twenty gives 208 candidates that match a
random crash ratio **one time in two**, which is no information at all; the minor-term rule
leaves 117 that match **one time in five**. Still a coincidence an operator will sometimes
see, which is precisely why the verdict never turns on it.

## What this does to the re-based-quote guard

`ProtectiveExitEngine._rebased` suspends a stop when a declared ex-date says the quote was
re-based, or when `price_discontinuity` sees a step over the band. The second rule is a
size test, and the arithmetic above is exactly why a size test cannot do this job: the
catastrophic gap that most needs a stop is silenced by the rule that protects a split.

The classifier narrows that in **one** case, and only where it can prove an action is
impossible. A corporate action re-bases at an open, so a step over the band between two
marks inside a single session was not one, and the quote still refers to the same unit the
stored stop does. There the stop stays armed. Suspending it leaves a genuinely collapsing
position naked for no reason the engine can state.

Everywhere else the suspension stands. Where a split and a crash are genuinely
indistinguishable, liquidating against a cost basis that may no longer mean anything is
the fabricated loss the guard exists to prevent, and the classifier has no more to offer
than the guard does. What changes is that the step becomes an open question with an
operator's name on it and a deadline. The verdict can never *create* a suspension, and it
cannot reach the declared-ex-date branch at all — a declaration classifies as
`DECLARED_ACTION`, never as an intrasession break.

### The suspension is one sweep, not a hold

Worth knowing before relying on either: `price_discontinuity` compares each mark with the
previous one, and the previous one is updated by the sweep that suspends. The next sweep
therefore sees 70 against 70, no step at all, and liquidates against the pre-adjustment
cost basis — the fabricated loss, one second late. Only a declared ex-date, re-asserted
from the calendar on every sweep, actually holds for a session.

`test_an_undeclared_discontinuity_suspends_the_stop_for_exactly_one_sweep` pins that
behaviour rather than changing it. Lengthening the hold leaves a genuinely collapsing
position unprotected for a whole session; shortening it books a loss the market never
caused. That is an operator's decision about their own book, not something to alter
silently underneath them — but it does mean the alert is the part that reaches a human in
time, and the suspension mostly is not.

## An unresolved gap cannot go quiet

Where a corporate action and a catastrophic gap genuinely cannot be told apart, the safe
behaviour is not to sit on it. It is to be loud.

* **Immediately.** The first unexplained step raises
  `TradingAlertCode.OVERNIGHT_GAP_UNEXPLAINED` through the existing dispatcher, carrying
  the symbol, both marks, the step, the ratio and the nearest action ratio.
* **Every 30 minutes after that.** The interval is
  `ProtectiveExitEngine.re_entry_cooldown`: an unexplained price is repeated at least as
  often as the engine would let itself re-enter a stopped-out symbol. Against the shortest
  regular session in `SESSIONS` (NSE, 6h15m) that is twelve alerts, so silence for a whole
  session is arithmetically impossible rather than merely unlikely.
* **A halt after one full session.** `regular_session_length` for the venue. At that point
  the market has been open for as long as it takes to explain the step and nobody has, so
  the book is being run on numbers no one has stood behind. `overnight_gap_unresolved:<symbol>`
  engages the kill switch.

Ordinary trading afterwards does not clear it. The gap happened; a quiet tick a second
later is not an account of it. Only an operator's declaration closes the loop — and when
one arrives, a closing alert says so, because an operator who was paged is owed the
close-out. A position leaving the book closes it too, which is why a symbol the very next
sweep liquidates raises one alert and then stops: the question went with the position.

A declared ex-date never becomes an open question at all. The operator already said what
it was, so there is nothing for them to resolve and nobody is paged about an event they
scheduled.

The halt blocks entries and never liquidates, for the same reason the unpriceable-mark
halt does: acting on a quote the engine has just admitted it cannot interpret is how the
ambiguity becomes a booked loss. Protective exits keep running throughout, against the
levels they always used.

## The exposure limits

### Overnight gross: 25% of equity

Derived, not chosen.

`AdversarialStressAgent.DEFAULT_SCENARIOS` already declares the worst gap this engine
models: `CRISIS_GAP_DOWN_8PCT`, −8%. The same agent already declares what one modelled gap
may cost the book: `book_tolerance_fraction`, 2% of equity, which is itself
`RiskPolicy.max_daily_loss`, the intraday loss breaker. The book stress veto ties them
together on the reasoning that the engine must not add exposure that one modelled crisis
day would, on its own, turn into a halt.

Overnight that reasoning gets *stronger*. Intraday the stop stands between the move and
the book; across a close nothing does, so the whole of the modelled gap lands.

```
2% book stress tolerance / 8% worst modelled gap = 25% of equity
```

The cross-check is the one the book veto passes too. Five positions at the 5%
`RiskPolicy.max_single_trade_notional` cap are exactly 25%, and five is the default
`max_open_positions`. The cap permits precisely the book the engine was built to hold
overnight and refuses anything past it.

It is strictly tighter than the 45% correlation-adjusted gross limit and the 60% notional
gross cap, so it only ever subtracts from what those already allow.

Rejection reason: `overnight_gross_limit`.

### The closing window: one cadence interval

An entry opened inside the last cadence interval of the regular session — ten minutes,
`AutonomousCadenceScheduler`'s default and the ghost runner's — gets no further cadence
tick before the close. The swarm sees it once and then it is an overnight position by
default rather than by decision, with no opportunity for the engine to reconsider it and
very little for the stop to act.

Those are refused outright rather than sized down. A haircut still leaves an unmonitored
position carrying a gap, and the honest answer to "we have no time to manage this" is not
to open a smaller one.

Rejection reasons: `overnight_closing_window`, and `overnight_entry_outside_session` for
an exposure-adding order placed when the regular session is not open at all.

## Arming, and which way it fails

Opt-in by data, exactly like the cross-position gates.

* **Unarmed.** No session calendar on the firewall, no `PRAMANA_OVERNIGHT_GAP_MONITOR`:
  neither half applies, the entry path behaves exactly as it did before, and the engine
  says nothing about a price step it could not account for. This is the pre-existing state,
  honestly labelled. `RiskWarden()` with no arguments is unarmed, which is why this
  increment changes no existing behaviour and no existing test.
* **Armed.** Every question the policy cannot answer is a refusal and never a pass: no
  timestamp, a tz-naive timestamp, a market with no session definition (`Market.GLOBAL`),
  non-positive equity, a calendar that raises. `OvernightExposureFirewall.evaluate` catches
  every exception and converts it to `overnight_risk_unavailable:<cause>`, logged at
  WARNING so an operator can see that the policy, not the book, stopped the trade. An
  operator who arms an overnight policy is asking the engine not to carry exposure it
  cannot reason about, so unable to reason means do not add.

The gap monitor fails the same way in the other direction: a naive clock, an unknown venue
or a raising operator source make it decline to judge and log, rather than invent a verdict
or raise into the one-second sweep. A raising alert sink is caught too — these alerts are
raised while something is already unclear about a protected position, and a sink that
raised would abort the sweep trying to report it.

Settings: `PRAMANA_OVERNIGHT_GROSS_CAP` (a fraction, e.g. `0.25`) and
`PRAMANA_OVERNIGHT_GAP_MONITOR` (`session`). A malformed value is a boot failure, not a
silently unarmed control: an operator who typed a cap meant to have one.

The cap is read in `risk.overnight.overnight_risk_from_env`, which
`agents.traded_runtime.build_traded_runtime` calls for every runtime that trades. The
daemon and the historical replay therefore cannot arm it differently — a replay that
ignored a limit the daemon enforces would draw a curve for an engine nobody runs, which is
what `tests/test_backtest_fidelity.py` exists to prevent. The monitor reads the same
`PRAMANA_CORPORATE_ACTIONS` calendar the suspension guard reads, so the two can never
disagree about whether today's quote was re-based on purpose.

**Risk-reducing orders never reach any of this.** `RiskFirewall` returns
`approved_risk_reducing` and `RiskWarden` returns on that verdict before the overnight gate
runs, so a covered exit is never trapped by an overnight limit, a closing window, or a
calendar that could not answer — at any hour, on any book.

## Nothing was loosened

Every change is a refusal added to a decision that already existed.

* The monitor's verdict travels back into `_rebased` for one purpose only: it may re-arm a
  stop the step guard would have suspended. It can never suspend one.
  `test_the_monitor_never_creates_a_suspension_the_guard_would_not_have` runs the same
  book and the same marks with and without a monitor and requires identical exits, and
  `test_a_step_that_cannot_be_a_corporate_action_keeps_its_stop` shows the narrowing
  firing in the only direction it can.
* The warden's overnight gate runs only after the firewall and the book gates have already
  approved, and can only turn an approval into a refusal.
  `test_arming_the_overnight_gate_never_approves_what_it_would_have_refused` checks that
  over every combination of held book, entry size and moment in the session, including
  outside the session and on a Sunday.

## Known limits

* **The daemon still does not square off at the close.** This makes overnight exposure
  measured and bounded; it does not eliminate it. Deliberate: end-of-day squaring is a
  strategy decision with its own costs, not a safety fix, and it belongs to whoever owns
  the strategy.
* **No gap-scenario leg was added to the adversarial agent.** The 25% cap is *derived
  from* the existing `CRISIS_GAP_DOWN_8PCT` leg; a second, larger gap leg would shock the
  same scenario twice and leave two numbers governing one thing.
* **−8% is a modelled scenario, not a bound.** The gaps this policy exists for are the
  ones that exceed it. The cap sizes the book so one modelled crisis gap does not by
  itself trip the loss breaker; it does not promise the gap will be −8%.
* **The classifier is evidence, not adjudication.** `UNDETERMINED` is the honest answer
  most of the time, and it stays an open question until a human closes it.
* **Nothing adjusts the ledger.** A declared corporate action resolves the alert; it does
  not re-base the stored stop or cost basis. That is a ledger change and a separate
  decision.
* **The one-sweep suspension was pinned, not fixed.** See above. Whether an undeclared
  discontinuity should hold a stop off for a session is a decision about the operator's
  own book, and both directions carry a real cost.
* **The monitor's marks are in-process.** A restart across the boundary loses the previous
  session's mark, and the first observation after it is never read as a step. That is the
  safe direction — it leaves every protective level exactly as armed as it already was —
  but it means a daemon restarted overnight will not see the gap it restarted across.
