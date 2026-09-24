# The probe path

A probe is a hold that Atlas turns into a small, labelled entry. The specialists
leaned BUY, but not strongly enough for a full entry. It exists so the book produces
directional decisions the calibration report can score. It is not meant to make the
book trade more.

A probe is sized at a fraction of equity, journaled with `probe=1` and labelled
`exploration_probe` in its proof. `pilot_ops.py calibrate` reports probes apart from
conviction entries. Every risk gate still applies to a probe.

## Settings

The operator sets all of these in the host `.env`. The build never raises the cap: it
is 0 in `AtlasPolicy` and in `deploy/docker-compose.yml`, and
`tests/test_probe_budget_telemetry.py` pins both.

| Setting | Default in the build | Allowed | What it does |
|---|---|---|---|
| `PRAMANA_EXPLORATION_MAX_PER_DAY` | `0` (off) | 0 or more | Probes per IST session, counted from the journal, so a restart does not reset them |
| `PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE` | `0.45` | above 0, up to 0.45 | The specialists' weighted BUY lean a probe needs. A full entry needs 0.45 whatever this is |
| `PRAMANA_EXPLORATION_MIN_CONFIDENCE` | `0.40` | above 0, up to the consensus floor | The specialists' average confidence a probe needs |
| `PRAMANA_EXPLORATION_NOTIONAL_FRACTION` | `0.01` | above 0, up to 0.05 | Probe size as a share of equity, whole shares only |

A malformed value is a boot failure, not a budget that is quietly switched off.

## The pilot (tenant `ghost`), as decided on 24 September 2026

- **Cap: 3 a day.** The founder kept 3.
- **Probe bar: 0.35.** Decided on 24 September; it applies from the first deploy after
  `PRAMANA_EXPLORATION_MIN_WEIGHTED_SCORE=0.35` is in the host `.env`. The strongest
  lean that day was 0.37 at 0.72 confidence, so the 0.45 default left the budget
  unused. Full entries still need 0.45.
- **Confidence and size:** whatever the host `.env` sets.

The running values, not this page, are the authority. Read them in either place:

- the **Exploration budget** panel on the dashboard's Overview;
- the `exploration` line of the pre-market check:

```sh
docker exec deploy-pramana-ghost-1 python /app/scripts/pilot_ops.py premarket --database /data/pramana.db
```

## What the Overview panel says

The engine publishes the budget it is running, taken from its live policy rather than
re-read from the environment, so a `.env` edit shows only after a restart. The count
for the day comes from the decision journal.

| Panel | Meaning |
|---|---|
| **Probes off: the cap is 0** | `PRAMANA_EXPLORATION_MAX_PER_DAY` is 0. Every lean below the full-entry bar stays a hold. |
| **Up to N probes a day** | The bar, the confidence, the size, the regimes that never probe, and "k of N used today". |
| **Probe budget not reported** | The engine is not running, or its report is stale, belongs to another account, or does not validate. This is never shown as off or on. |

## What never changes

- **A hard hold stays a hold.** A specialist veto, stale evidence or missing coverage
  records no consensus, and a probe needs one.
- **Some regimes never probe.** `trending_down` (defensive) and `high_volatility`
  (crisis stand-down) do not probe, whatever the budget.
- **Only holds become probes.** Every probe is a BUY on a long-only book. The budget
  never touches a full entry's floor, size or bar.
- **This page arms nothing.** Raising the cap is an operator edit on the host, followed
  by a deploy.
