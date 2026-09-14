# Invalid accounting and valuation handling

The paper engine rejects stored fractional, zero, negative, malformed or unsafe-integer quantities instead of truncating them. Quantities must remain exact in Python and the dashboard's JavaScript representation. Account capital, cash, cost basis, fill amounts and fees receive finite-value validation; positive/nonnegative requirements follow each field's accounting meaning. The final fill transaction validates its inputs and resulting cash/position before committing. Rejection preserves cash, positions, fills, costs, proofs and retry keys.

## Protection can continue without inventing a position

Complete-book reads fail on an invalid position. A separate protective-exit read projects only rows with valid instrument identity, whole quantity and positive finite cost basis. Every excluded row remains unchanged in SQLite and appears in the bounded protection-coverage report. The existing durable halt blocks new pilot risk. A valid holding can still exit independently; the engine never guesses a corrupt holding's quantity or basis. Invalid account cash prevents every fill, including sells, because a valid cash result cannot be established.

The normal pilot factory can restart with in-scope damaged position quantity/basis or damaged cash and continue reporting the fault. It reads valid starting capital without calculating gross exposure first. Invalid starting capital, out-of-scope identity, damaged schema and other startup dependencies can still prevent startup; this is not automatic database repair. Such failures must be caught by target-host health and recovery monitoring.

Protective marks must be finite and positive. The tick-reader boundary independently rejects future observations and observations older than 120 seconds. Invalid buffered prices/volumes cannot poison aggregation or crash bar callbacks. This does not establish complete feed ordering, exchange-time accuracy, source adjustment or corporate-action correctness.

## Withhold totals and retain the failed observation

An unusable account or valuation publishes a current paper-runtime heartbeat with `valuation.status = unavailable`, a fixed reason, tenant/ledger context and an entry halt. The stored valuation has `status = invalid`, null financial totals, no holdings, and false freshness/session/strategy eligibility. No partial book is used to report equity or size new risk. The first halt reason remains latched.

An invalid minute cannot be overwritten by a valid sample after same-minute source restoration. The following valid minute may publish normally. Any recorded invalid minute excludes that day from account and applicable strategy observation qualification. These samples establish software behavior only, not profitable strategy evidence.

The private workspace remains available. Overview/Portfolio show **Portfolio totals withheld**; Risk lab withholds portfolio risk; the watchlist and navigation remain usable. The equity curve preserves null gaps and explicitly disables connecting across them. Aged invalid observations keep their invalid status. The normalized UI portfolio uses its existing invalid-state placeholders, but never renders those placeholders as current financial totals. Restoring valid source records does not release the entry halt.

## Verification and remaining gates

All **627 Python, 64 dashboard and 13 Worker tests** pass, along with required/changed-script Ruff, TypeScript and the production dashboard build. New cases cover malformed positions/cash/capital, atomic rejection and retry preservation, real-factory restart, independent valid exits, future/old/nonfinite marks, bad buffered ticks, persistent invalid minutes, and exclusion of a damaged day despite more than 300 other valid observations.

A disposable real paper engine and compiled private dashboard were exercised through invalid basis, restart and explicit restoration of the fixture's original value. API/browser checks confirmed withheld portfolio/risk totals, usable watchlist/navigation, retained null chart gaps, and a halt still acknowledged after restoration. No browser warnings/errors were captured, and temporary processes/tabs were closed. The Linux verifier adds the same invalid/restart/restoration phases to actual deployment images; its revision-specific CI artifact must pass before image verification is claimed.

No original deployment data were repaired, no provider/AI request or real order was made, and no acceptance was signed. Real open-session feed/protection parity, target-host startup/alerts/token renewal/soak/encrypted off-host recovery, strategy holdout/forward validation, external brokerage lifecycle and wider benchmark capabilities remain open. This change provides no guarantee of profit or maximum realized loss.
