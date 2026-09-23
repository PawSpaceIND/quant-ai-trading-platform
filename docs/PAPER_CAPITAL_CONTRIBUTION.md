# Adding capital to the paper account

The paper account is funded once, the first time the ledger sees the tenant. Raising
`starting_capital` in the founder directives afterwards changes the risk plan but not the
book: the account row already exists and is never re-created, the position sizer works
from the ledger's equity, and a hand edit of the cash fails reconciliation. A recorded
capital contribution is the supported way to add capital.

## What a contribution changes

One SQLite transaction on the live ledger (`scripts/pilot_ops.py capital-contribution`,
implemented in `quant_ai.execution.capital_contribution`):

| Figure | Change | Why |
|---|---|---|
| `paper_accounts.starting_capital`, `cash_balance` | both + X | every replay still finds cash = capital + fills - costs |
| `paper_accounts.peak_equity` | + X | the rupee drawdown already suffered is kept, not erased |
| today's (UTC) `risk_daily_equity` opening | + X | the deposit is not the day's profit, so the daily-loss breaker stays live |
| `paper_capital_contributions` | one append-only row | amount, reason and every before/after figure |

Reconciliation runs inside the transaction before and after the change. If either fails,
nothing is written. A `--reference` names the contribution; running the same command
again records it once, and reusing a reference with another amount is refused.

It is refused while `TRADING_LIVE_MONEY_ACTIVE` is anything but `false`, and during NSE
regular hours. Amounts are whole rupees, above zero and at most one crore per contribution.

## What it does not change

* The founder directives. The plan's rupee limits come from `starting_capital` there; set
  it to the new total in the same change. `pilot_ops.py premarket` prints a
  `capital_plan` line that fails while the plan and the ledger disagree, and the command
  itself exits 3 when they do.
* The double-entry trading journal (`quant_ai.accounting`). The paper pilot does not
  write one. An operator running that mode must post the matching CAPITAL transaction
  (debit `CASH_AVAILABLE`, credit `CAPITAL`) to it.
* History. Valuations published before the contribution keep the old starting capital,
  and the equity curve steps up on the day of the deposit. The dashboard's performance
  panel accepts a changed starting capital only where a recorded contribution explains it
  (any other change still invalidates the observations), keeps every qualified session,
  and time-weights its returns: the deposit is taken out of the day it lands in. The
  per-instrument contribution panel divides by the current capital, so its percentages
  shrink after a top-up. A run comparison whose window spans the contribution reports the
  earlier valuations as mismatched; choose a window that starts after it.
* `scripts/india_paper_runtime.py`, a separate launcher with its own directives file and
  its own one-lakh certification.

## Runbook (IST, after the close)

1. Merge the change that raises `starting_capital` in the directives file the engine
   mounts, then deploy it (`./scripts/deploy_pilot_host.sh`; the script refuses during
   NSE hours).
2. Record the contribution inside the engine container, with the ledger path it uses:

   ```sh
   docker exec deploy-pramana-ghost-1 python /app/scripts/pilot_ops.py capital-contribution \
     --database /data/pramana.db --amount 900000 --reference topup-YYYY-MM-DD \
     --reason "scale paper book so every watchlist name fits the trade cap"
   ```

   Expect `"recorded": true`, the before and after capital, and
   `"planMatchesLedger": true`. A repeat prints `"recorded": false` and changes nothing.
3. Restart the engine so it starts from the new figures, then confirm:

   ```sh
   docker restart deploy-pramana-ghost-1
   docker exec deploy-pramana-ghost-1 python /app/scripts/pilot_ops.py reconcile --database /data/pramana.db
   docker exec deploy-pramana-ghost-1 python /app/scripts/pilot_ops.py premarket --database /data/pramana.db
   ```

   Reconcile must say `matched`; the premarket `capital_plan` line must be OK.

Probe size is a separate setting, `PRAMANA_EXPLORATION_NOTIONAL_FRACTION` in the host
`.env` (a fraction of equity, at most 0.05). A probe buys whole shares only, so the
fraction must cover one share of the dearest name the pilot should be able to probe.
