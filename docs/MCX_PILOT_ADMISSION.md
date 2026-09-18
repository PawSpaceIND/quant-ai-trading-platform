# MCX paper-pilot admission

This change opens one previously hard-closed pilot gate. It does **not** invent a
fee, margin, contract, token, or production commodity watchlist.

The private pilot may include an Indian MCX instrument only when the instrument is
an INR `METAL` or `COMMODITY` dated contract and all existing contract, fee,
margin, and bound-identity controls are satisfied. NSE cash equity/ETF behavior is
unchanged. NFO/BFO futures, options, currency derivatives, NCDEX, MSEI, IFSC, and
non-INR instruments remain outside this admission.

## Reused controls

The implementation deliberately reuses the existing components rather than
building parallel derivative rules:

- `Instrument` and `instruments.identity` carry and persist expiry, lot size,
  tick size, underlying, exchange, currency, metadata, and exact contract identity.
- `assert_contract_tradable` refuses new risk during rollover and after expiry.
- `assert_order_fits_contract` refuses non-lot quantities and off-tick prices.
- `DerivativeFeeSchedule` contains no built-in MCX numbers. Admission requires a
  schedule that reconciled to an operator-supplied contract note.
- `DerivativeMarginSource` contains exact broker-sourced per-contract collateral,
  with an operator-selected maximum age. Missing, stale, future, or mismatched
  evidence refuses new derivative risk.
- `MarketFrictionModel` and `PaperBrokerService` retain fee, collateral, and exact
  contract evidence on the paper fill.
- `InstrumentBoundOrderIntent` is mandatory for admitted MCX risk. A legacy
  unbound order cannot use an MCX pilot scope.
- The existing exchange calendar continues to select MCX session hours. The
  seasonal-close follow-up below corrects its reversed US-DST/standard-time values.

## Admission versus execution

Admission is a precondition, not a permanent authorization. At pilot
configuration time the selected contract must be LIVE, the fee schedule verified,
and the exact margin snapshot usable. At execution time the broker checks the
contract again. A contract configured earlier but expired later cannot fill.

The pilot scope stores the full canonical instrument identity. A different expiry,
lot, tick, underlying, metadata, market, or asset class under the same symbol is
not silently adopted. Existing MCX positions without a bound identity are not
migrated into pilot scope.

A valid fee schedule and margin source do not certify a market data subscription,
instrument token, strategy edge, liquidity, or exchange entitlement. Those are
separate deployment and acceptance gates.
## Runtime identity requirement

Any watchlist containing MCX requires `PRAMANA_ORDER_IDENTITY_MODE=bound_v1`.
Broker pilot configuration refuses MCX unless the matching bound runtime identity
has already been persisted. This check precedes writing the MCX pilot scope; it
is not a claim that the broker database or schema has never been constructed.
A legacy `OrderIntent` cannot bypass the bound entry authority merely because its
symbol appears in the pilot watchlist.

The early validator reuses the existing `DerivativeFeeSchedule.from_env()` and
`DerivativeMarginSource.from_env()` selection rules. At broker configuration, its
actually selected fee schedule and margin source are passed explicitly to admission.
An explicit missing source is not replaced from the environment.

## Configuration boundaries

The fee schedule uses the existing `PRAMANA_MCX_FEE_*` fields. Do not copy the
synthetic values from tests. The schedule is admitted only after its supplied
contract-note reconciliation matches under the supplied tolerance.

The margin source uses the existing `PRAMANA_DERIVATIVE_MARGIN_FILE` or
`PRAMANA_DERIVATIVE_MARGIN_JSON`. Do not configure both. Each record must identify
the exact symbol, market, asset class, lot size, SPAN amount, exposure amount,
source, observation time, and maximum evidence age.

This PR contains no production fee schedule, margin amount, contract token, or
tradable MCX symbol. That is intentional. If authoritative evidence is not
available, the correct operating state is refusal.
## Required acceptance before any MCX deployment

Repository tests alone do not admit a real contract. Before an owner changes the
production watchlist, verify all of the following on the target host:

1. Paper-only mode remains active and `bound_v1` is selected.
2. The exact MCX contract came from the current licensed instrument master and its
   token/symbol mapping matches the live feed.
3. The fee schedule is operator-reviewed and reconciles to an applicable real
   contract note.
4. The margin record is for that exact contract/lot and is within its declared age.
5. The contract is outside rollover and not expired.
6. The current MCX holiday/special-session configuration is correct for that day.
7. Live quotes are fresh during MCX hours and protection continues after NSE cash
   closes.
8. A paper lot-sized entry and covered exit reconcile fees, collateral, position
   identity, accounting, and protection without any live-money path.
9. Restart with the position open preserves the exact contract and reserved
   collateral; a substitute expiry is refused.
10. Independently verify alerts and restore evidence before expanding the operating
    scope further.

No step above authorizes options, currencies, other exchanges, or live money.
## Verification

`tests/test_mcx_pilot_admission.py` checks positive and refusal behavior using
synthetic contracts and synthetic fee/margin evidence. These numbers are fixtures,
not operational recommendations.

`tests/test_mcx_pilot_guards.py` deliberately removes named protections on disposable
source copies. Each unchanged control must pass and each mutant must produce a
specific assertion failure. The campaign includes verified fees, current/matching
margin, contract identity/lifecycle, exchange/currency scope, bound runtime/order
identity, existing-position migration, lot size, and expiry checks.

Owner review and a fresh exact-head full suite/CI are required before merge. The
owner, not this development lane, decides whether and when to deploy.

## Corrected normal-session seasonal close

The inherited session table had its seasonal closes reversed: it used 23:55 IST
in US daylight saving and 23:30 in standard time. The exchange's Trade Timings
specify 09:00–23:30, extended to 23:55 typically November–March. The table now
uses 23:30 during US DST and 23:55 in standard time; there is no MCX post-market
trading window. Regular close is exclusive, including on equivalent UTC inputs.

Primary source checked for this repair:
https://www.mcxindia.com/market-operations/trading-surveillance (Trade Timings).

The existing fixed standard-time annualisation convention consequently uses
895 normal-session minutes, not the previously incorrect 870. This is not a
count of actual historical sessions; reports still need real interval/calendar
metadata. No performance threshold, trading limit or statistical floor changed.
The original session tests incorrectly encoded the old reversal; their affected
expected results and inside-closing-window fixture now follow the exchange rule.

Normal weekday boundary and overnight-firewall tests cover winter and summer.
This does not qualify special/holiday half-days, agricultural contract sessions,
future exchange-circular changes or actual evening-feed/protection operation.
Do not enable MCX on a running account solely because these tests pass.
