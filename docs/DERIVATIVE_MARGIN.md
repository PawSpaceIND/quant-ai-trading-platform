# Futures margin accounting

The paper ledger models futures margin as **cash collateral, not purchase notional**. An
entry reserves the exact SPAN + exposure margin supplied for that exact contract by a
broker-sourced margin snapshot. No percentage-of-notional fallback exists. If that exact
margin row is absent, malformed, stale, from the future, or the order is not a whole number
of exchange lots, new risk is refused. Every source must declare an explicit
`maxAgeSeconds`; the engine does not invent a freshness window.

Risk remains measured on **full marked notional**. `max_single_trade_notional`, symbol,
asset-class and gross-exposure caps do not shrink because the broker asks for less cash.
`max_leverage_multiple` is a separate **projected-book** notional/equity ceiling, so
several individually small futures orders cannot accumulate past it when the ordinary gross
cap is widened. A margin shortfall blocks new risk only; it never creates a forced liquidation
and never traps a covered exit. New futures shorts are explicitly unsupported by this
long-only paper ledger and refuse before submission.

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

## Admission versus final cash check

The risk firewall validates sourced margin against the snapshot's available cash. The paper
broker remains the final transactional authority and also subtracts the exact cash charges
computed by the verified friction/fee schedule. A pre-trade approval is therefore not a
guarantee of fill: if margin plus cash fees no longer fits at submit time, the transaction
refuses atomically and no fill/cash mutation is committed. This is intentionally fail-closed
until fee estimation is promoted into the common pre-trade risk contract.

`BrokerMargin.gross_position_value` is an account-value input, not the risk engine's futures
notional. Cash positions contribute entry cost and futures contribute reserved collateral;
full derivative notional lives in `PortfolioSnapshot.gross_exposure`.

OTC/spot `AssetClass.FX` is intentionally excluded from the futures-margin class set because
`OrderIntent` still lacks exchange/contract identity. Currency futures remain out of pilot
scope until the contract-aware order model can distinguish them without a global fill-time
lookup.
