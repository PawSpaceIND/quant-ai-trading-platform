# Futures margin accounting

The paper ledger models futures margin as **cash collateral, not purchase notional**. An
entry reserves the exact SPAN + exposure margin supplied for that exact contract by a
broker-sourced margin snapshot. No percentage-of-notional fallback exists. If that exact
margin row is absent, malformed, or the order is not a whole number of exchange lots, new
risk is refused.

Risk remains measured on **full marked notional**. `max_single_trade_notional`, symbol,
asset-class and gross-exposure caps do not shrink because the broker asks for less cash.
`max_leverage_multiple` is a separate hard notional/equity ceiling. A margin shortfall
blocks new risk only; it never creates a forced liquidation and never traps a covered exit.

## Mark-to-market choice

This paper model **holds futures P&L unrealised between marks** instead of simulating daily
exchange variation-margin settlement. Free cash therefore does not move merely because a
mark changed. Equity is free cash + reserved margin + unrealised futures P&L. On a close,
the ledger releases the proportional reserved margin and realizes the closed quantity's
P&L into cash. This is an explicit modelling choice, not a claim that a real clearing
account postpones daily settlement.

Every fill stores its margin reservation/release. Reconciliation replays those historical
cash effects rather than asking today's margin source, because a broker can change SPAN or
exposure requirements after the fill. The current reserve also stores the source
provenance. This remains internal paper-ledger reconciliation, not external broker proof.
