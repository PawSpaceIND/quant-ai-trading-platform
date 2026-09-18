# Post-126 institutional paper risk repairs

Branch: `fix/post-126-risk-closure`. Base: `3cfd99dda203fd3688906fa22b900e4e25214f6b`.
This change addresses the four failing acceptance requirements only. It does not
assemble the complete institutional daemon, connect data providers, change account
configuration, enable live money or claim profitable trading.

## After-cost Kelly arithmetic

Use `net_win = average_win_return - expected_cost_return` and
`net_loss = average_loss_return + expected_cost_return`. With conservative probability
`p`, the uncapped loss-budget fraction is `max(0, p - (1-p)*net_loss/net_win)`.
A nonpositive net win produces zero sizing. Existing evidence, expectancy, policy
haircuts and hard caps remain conjunctive. This fraction is a modeled loss budget,
not a direction to invest that fraction of cash at full notional.

The exact acceptance example is p=0.6, gross win=0.02, gross loss=0.01 and cost=0.005.
Net payoffs are 0.015/0.015, so raw Kelly is 0.2, not the previous 0.4. No calibrated
probability or cost is estimated by this patch; declared evidence is still required.

## Whole-parent modeled-loss allowance

Before planning and before each due child, compare current equity times the edge
recommendation against the larger of (a) full-parent stop-distance loss and
(b) full-parent notional times empirical adverse return plus declared costs.
This preserves the original acceptance definition, including its exact boundaries.
A tighter stop cannot hide the larger empirical adverse payoff. The full approved
parent quantity is checked again after earlier slices and after restart; a child
never receives an independent copy of the parent's modeled-loss allowance.

This is a proxy, not a guaranteed bound on execution loss, gap loss or total costs.
It takes the maximum of two models; declared costs are included in the empirical
payoff term. It does not claim to bound stop execution plus every transaction cost. Actual market behavior still requires forward evidence. The guard
re-evaluates the configured edge policy; immutable edge-policy version pinning and
cross-program risk-budget reservation remain separate governance requirements.

## Complete factor-book input binding

The portfolio's per-symbol quantities and marked exposures must cover the same
symbols, have valid nonnegative measurements and agree with its gross exposure.
The factor input must exactly represent all those holdings plus the proposed
purchase, with no duplicates, omissions, changed quantities or substituted values.
The same check runs against the fresh snapshot and factor provider at each slice.
Malformed provider data and declared provider failures refuse before submission.

This uses the existing long-only, symbol-keyed portfolio representation. It does
not invent short positions, missing marks, factor coefficients, contract metadata,
FX conversion or provider authenticity. It validates consistency with the supplied
snapshot, not independent truth of the snapshot. Broader contract/routing and
source qualification remain separate requirements.

## Strategy exposure and exits

Strategy exposure must be a finite, nonnegative Decimal at preparation and dispatch.
Booleans, numeric strings, floats/integers, missing values, NaN/infinity, negatives
and declared provider errors produce `strategy_exposure_measure_unavailable`.
The preparation allocation check now also includes already used strategy exposure.

Covered sales bypass these entry-only checks. Existing Warden controls still run;
independent protective exits, broker receipts, OMS, accounting and halt semantics
are unchanged. Failed guards never submit another order or make an automatic retry.

## Regression evidence and fixture repairs

The untouched 17-case acceptance suite reproduced 15 failures and two passes on
`3cfd99d...`. It now passes all 17 cases. The new regression file adds 42 cases,
including exact net-payoff arithmetic, malformed strategy data at both stages,
provider exceptions, live-slice projection mismatch, actual existing holdings,
restart after a partial TWAP, and covered exits with broken entry-only providers.
The focused cross-module run passes 210 tests.

Two existing fixtures were made consistent with the new checks, with every existing
assertion preserved. The factor-stress case now changes factor loading on the true
quantity instead of inventing 700 shares. The protection/recovery case now includes
its first committed holding when projecting the second purchase. The first full
run recorded 2377 passes and that fixture failure; it is retained as intermediate
evidence, not represented as the final passing result.

Four isolated in-memory mutations removed cost-adjusted sizing, parent-risk checking,
factor projection binding and strategy-input validation. Each caused the relevant
original acceptance tests to fail. No certified source file was modified during
these experiments. The original acceptance file remains byte-for-byte unchanged,
and the existing coordinator/recovery assertion trees were compared unchanged.

Exact final local and GitHub results are recorded in the risk-only pull request.
These tests prove the covered synthetic invariants, not real-feed quality, optimal
trade sizing, execution guarantees or a profitable edge. #126 and main are not
merged by these changes; rollout and broader launch acceptance remain separate.

A later full Mac run hit storage failures in unrelated research-dashboard tests:
SQLite could not open a new temporary database and pytest could not create a new
temporary directory. All six tests in that unchanged research file passed with a
dedicated temporary root. Full dedicated-root and GitHub certification results are
reported separately in the PR; neither failure is silently relabelled as a pass.
No test was removed or assertion weakened to accommodate the host condition.

During certification, main advanced to `58a3d72a6fafbd17df97bccb78943a30be06df48`
via #134 (cadence tick cutoff). Its five changed files do not overlap this risk patch.
This risk-only PR remains based on #126's unchanged integration head. Refresh and
certify #126 against then-current main after the risk patch is reviewed; this branch
does not silently incorporate unrelated upstream changes.
