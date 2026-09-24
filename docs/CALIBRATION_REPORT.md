# Reading the calibration report

`pilot_ops.py calibrate` answers one question from the decision journal: has the forecast
earned the right to be trusted, and if not, what is missing. It is read-only, arms nothing,
and exits 0 whenever it could produce the report. The verdict is in the text.

```sh
docker exec deploy-pramana-ghost-1 python /app/scripts/pilot_ops.py calibrate --database /data/pramana.db
```

## Sections, in the order printed

| Section | What it says | When it concludes nothing |
|---|---|---|
| Basis integrity | Per forecast basis: rows whose own inputs reproduce the stored probability (`verified`), rows from a single-producer path (`by_path`), and rows that prove a second mapping (`contaminated`). Any contaminated row makes the verdict `basis_mixed`. | Never; a mix always fails. |
| Scored sample | Resolved clean forecasts, Brier score, and Brier for a coin and for the base rate. | Below 30 scored, skill is not printed; below 200 the verdict is `insufficient_sample`. |
| Reliability | For each probability bin: how many forecasts, their mean stated p, and how often the move happened. | Marked `(thin: no claim)` below 30 scored. |
| Book | Filled buys, mean net P&L on them, median absolute entry drift, realised max drawdown and the live drawdown limit. | A missing drawdown or limit refuses the verdict (`missing_inputs`). |
| Entries | Conviction entries and labelled probes apart: fills, settled fills, mean realised net P&L (rupees), and mean EV at declared and at measured payoffs (return on notional). A probe is only a row the journal labelled `probe=1`. | A group with no settled fill shows `-`. |
| Payoffs | Measured `e_win_hat` / `e_loss_hat`, the break-even probability at declared payoffs (derived from the rows, 0.70 at 1% / 2% and 10 bps) and at measured payoffs, and the mean EV of every forecast under each. | Measured EV uses a symbol or playbook group only when it is not thin (30 rows); otherwise it shows `n=0`. |
| Journal | Rows, forecasts, forecasts the resolver has finished, last decided and last resolved time. | If `last resolved` stops moving while `last decided` moves, forecasts are no longer being graded. |

## What never happens

- Measured payoffs never size, gate or replace anything. The live expected value keeps the
  specialists' declared numbers; the measured column is there to be compared.
- The verdict never passes on a partial report. `pass` needs at least 200 clean resolved
  forecasts, a Brier score under the coin's, positive mean net P&L on filled buys,
  journaled entry drift, and a realised drawdown within the live limit.
- A `pass` does not arm the EV gate. That stays an operator decision (`PRAMANA_EV_GATE`,
  default off).
