# Publish research evidence: item 5

## Scope

`python -m quant_ai.cli publish-research` emits the existing `pramana.research.v1`
contract consumed by `apps/pramana-ui/lib/research.ts`. The reader is unchanged.
This is offline price-only momentum research, NOT the traded AI. It builds no
trading runtime, calls no model or broker, and writes no orders. It requires
`TRADING_LIVE_MONEY_ACTIVE=false` (an absent setting defaults to false here).

The fixed plan uses the first 70% of filtered daily bars for training and the last
30% for holdout. Candidate lookbacks are 5, 10 and 20. Only training chooses among
candidates with at least 20 closed training round trips, by highest after-cost
return, with a shorter-lookback tie-break. The selected rule and buy-and-hold
restart flat with equal capital on the identical holdout. Training positions and
warmup bars are not carried into holdout. A failing holdout cannot select a new
winner: the publication refuses. At least 30 return observations and 20 closed
rule round trips are mandatory.

The complete earlier trial register is required, using the existing
`paths.trial_register("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")` resolution and
`replay:{symbol}:{market}` study. Every attempted candidate is registered before
selection. Counts include prior backtest/contest sweeps. Failed evaluations remain
counted. Missing/corrupt history, unknown prior windows and recorded prior windows
that overlap holdout dates refuse. Dates are venue-local, including IST for India.
A publisher lock serializes publishers, not legacy backtest/contest commands:
exclude other research writers operationally. Detected register drift refuses.

### Trial reservation and concurrent writers

The publisher pins the register bytes BEFORE checking prior studies and holdout
exposure. After reserving its candidates, it requires the register to be exactly
those original bytes plus its own returned canonical evidence record and newline.
A valid competing append just before or just after that reservation refuses with
`research_trial_register_changed_during_read`, before training or holdout runs.
The existing stable-read and final pre-publication drift checks remain active.
This closes a gap where a post-reservation snapshot could previously adopt a
concurrent writer's newly inspected holdout as though it had already been screened.

A refusal preserves the previous report and does not roll back either writer's
trial records. Reserved but unscored candidates stay counted conservatively.
A changed register must be reviewed, not reset to make the command pass.

This is detection at explicit boundaries, NOT a transaction or a shared lock with
legacy writers. An uncooperative write after the final check cannot be excluded
by this publisher alone. Continue to exclude other writers for the entire run;
wholesale replacement and unrecorded external inspection are not attested here.

## Historical data and an honest research plan

The handoff says the Lightsail host has no historical dataset or prior register.
That remains an external prerequisite, not evidence this PR has supplied.
The existing history producer writes the expected file shape, unchanged:

```sh
python scripts/fetch_historical_bars.py --symbols INFY --years 19 \
  --out-dir var/replay-datasets
```

Expected local dataset: `var/replay-datasets/INFY.json`.
Expected container dataset: `/data/replay-datasets/INFY.json`.
Preserve and import the COMPLETE earlier register to the publisher's resolved
register path. Do not reset it, create fictional entries, use an empty temporary
register, or switch paths to conceal previous research.

Fetching a series is not validation. The already-inspected 19-year INFY series
cannot become untouched by choosing a split today. Freeze the symbol, input hash,
start/end range and the fixed plan BEFORE inspecting genuinely unobserved holdout
outcomes. For a genuinely new study, any initial backtest must be restricted to
its training portion, and must be an actual registered run. Do not run a
full-sample contest to initialize history: that would expose the holdout.
The current publisher deliberately refuses a missing existing study rather than
inventing a trial count.

After preserving history and approving the plan, the offline command is:

```sh
TRADING_LIVE_MONEY_ACTIVE=false \
PRAMANA_RESEARCH_REPORT=var/research-report.json \
python -m quant_ai.cli publish-research \
  --data var/replay-datasets/INFY.json --market india
```

Optional `--start YYYY-MM-DD` and `--end YYYY-MM-DD` filter the dataset by venue-local
date BEFORE the fixed split. They must be part of the frozen plan, not tuned to
holdout results. The default output is `research-report.json` beside the resolved
paper ledger, exactly the existing dashboard convention. Publication refusals
exit nonzero and leave the previous report unchanged; attempted trials may still
be recorded. This publisher will not reuse a previously consumed holdout.

## Owner-only host operation, after review and deployment

These are instructions, not actions performed by this PR. The repository is
`/home/ubuntu/quant-ai/quant-ai-trading-platform`; `.env` is at its root.
Use the owner's normal clean-main deployment process. Do not deploy a feature
branch or force a mid-session rebuild for research.

From the repository root, an already-approved image can run the offline tools
without starting or restarting the daemon:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml run --rm --no-deps \
  --entrypoint python pramana-ghost /app/scripts/fetch_historical_bars.py \
  --symbols INFY --years 19 --out-dir /data/replay-datasets

docker compose --env-file .env -f deploy/docker-compose.yml run --rm --no-deps \
  --entrypoint python pramana-ghost -m quant_ai.cli publish-research \
  --data /data/replay-datasets/INFY.json --market india
```

Import the complete register and freeze the research plan before the second
command. The output is private (0600); run as container user `pramana`, uid 10001,
which the dashboard also uses. Confirm volume ownership and the authenticated
Research page on the actual host. No Compose edits are needed for the primary
panel's existing `/data/research-report.json` default.

## Evidence and limitations

The loader uses the history producer's actual PRICE_SERIES, ADJUSTMENT_POLICY
and INTERVAL values; it does not relabel producer output. One byte snapshot binds
identity, bars and input hash. Duplicate keys, adjusted-close declarations,
missing identity/source, contradicting market, unclosed daily bars and output
paths that overwrite input evidence refuse. Source declarations/hashes are not
independent evidence of data authenticity or licensing.

`data_sha256` follows CLI timestamp/OHLCV hashing. `source_file_sha256` binds all
input bytes. `report_sha256` hashes canonical UTF-8 JSON excluding only itself:
sorted keys, separators `(',', ':')`, ASCII escaping, no nonfinite numbers.
Training/holdout and register snapshot hashes are included. Economics remain
Decimal; exact equity/cost values accompany finite presentation numbers.

The cost model is reused, not recalibrated here: applying one built-in schedule
through history is NOT a sourced date-by-date historical statutory schedule.
Uncredited dividends, upstream split adjustment, survivorship, sample selection,
daily-bar spread/impact assumptions and unpaid exit costs on final open holdings
are disclosed. These are not traded-AI returns or proof of profitability.

Path stress is a seeded circular five-bar bootstrap of net holdout returns:
1,000 compounded paths, nearest-rank p95 maximum drawdown. It is not order
re-execution, a forecast or a bound on unseen tails. Its p95 may legitimately
equal observed drawdown. The independent regression uses a path where replacing
resampling with observed drawdown is detectably wrong.

## Sibling panels deliberately not populated

| Setting | Evidence missing from this publisher |
| --- | --- |
| PRAMANA_RESEARCH_LAB_REPORT | Matched provider experiments, resolved outcomes and measured API costs. |
| PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT | Comparison, simulation and company-event exports. |
| PRAMANA_PORTFOLIO_RESEARCH_REPORT | Continuous multi-asset simulation, attribution and reconciled books. |

No placeholder reports are emitted for these panels. Their existing workflows
retain ownership; the primary panel does not establish parity with them.

## Verification and external acceptance

Run `ruff check src tests` and the complete `pytest -q` suite. The requested Mac
binary `/usr/local/bin/ruff` was previously reported absent; no fresh Mac result
is claimed. GitHub's Python 3.12/Linux CI supplies the complete-suite evidence
recorded in the PR. Earlier temporary-workspace counts are not certification
of a later head.

Three mutation campaigns are part of the full suite:

- `tests/test_research_publisher_sabotage.py`: 18 outcome mutations, including
  fake holdout, reset trial counts, wrong schema and observed drawdown as p95.
- `tests/test_research_publisher_guard_inventory.py`: 57 mutations covering all
  52 explicit `_require` sites, three rejection handlers and two delegated
  daily-input validation boundaries. New or removed sites require inventory updates.
- `tests/test_research_publisher_reservation.py`: two additional mutations remove
  only the expected-append condition, leaving the old drift guards active. Each
  actual valid-chain race must then fail its named rejection test with DID NOT RAISE.
  A separate single-writer control proves ordinary publication still works.

Each mutation requires a passing control and a specific failing regression on an
isolated modified copy, with no collection errors. The reservation and inventory
campaigns also reject skips. Copies are restored and production-source bytes or
hashes checked. The PR records observed results for its actual tested checkout;
these campaigns are not exhaustive coverage of every possible fault or interleaving.

Still external: the complete historical trial register, authentic eligible data,
predeclared untouched holdout, independent cost/data review, target-host volume
permissions and authenticated Research-page acceptance. Do not merge or deploy
from this task, activate live money, or call this 100% launch closure.
