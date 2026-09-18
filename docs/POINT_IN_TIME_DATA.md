# Point-in-time data

Requirement `D02` in the capability register asks for "point-in-time historical data" with
"lookahead guards", and names `licensed_point_in_time_source` as an external dependency.
`quant_ai.marketdata.point_in_time` is the structure and the checks. It is not the data.

## Two different biases

**Survivorship** — a universe assembled today contains the companies that still exist. The
ones that were delisted, merged away or went to zero are absent, so a backtest cannot buy
them and cannot lose on them. The effect always points upward.

`PointInTimeUniverse` keeps delisted names with the window they were tradeable in, so
`members_on(day)` answers what could actually have been bought on that day. Asking outside
the universe's declared coverage raises rather than returning an empty tuple: the answer is
unknown, not "nothing was listed".

**Lookahead** — a price series fetched today carries today's values, including revisions and
adjustments that were not knowable at the time. `close_known_at` returns the value as it
stood at a given instant, and `None` when nothing was known. A caller must treat `None` as
missing data; substituting the current value is exactly the lookahead the function prevents,
and it is invisible in results.

## The survivorship audit

`PointInTimeUniverse.audit()` returns one of three verdicts:

| Verdict | Meaning |
|---|---|
| `plausible` | The universe loses names at a believable rate. |
| `implausibly_clean` | Below 0.5% a year over three years or more. Probably missing failures. |
| `survivor_only` | Not one delisting on record. The signature of a modern-constituent list. |

The 0.5% floor is a smell test, not a law — venues and eras differ — which is why it is a
named constant and a parameter rather than a hidden magic number. What is not a judgement
call is the zero case: an equity universe that lost nothing across years did not have the
failures removed by good luck.

Only `plausible` satisfies `usable_for_research`, and
`quant_ai.validation.harness.validate_candidate` refuses to clear a study on anything else.

## What this repository currently feeds it

Nothing yet. `backtesting/history.py` fetches Yahoo Finance daily bars for the watchlist in
the founder directives. That is not a universe — it is a list chosen today by an operator who
knows which companies still exist, which is survivorship bias in its strongest form, before
the vendor's own missing delistings are even considered. Yahoo also serves adjusted values
with no record of when an adjustment was applied, so it cannot answer `close_known_at`
honestly for any past date.

Populating this module is blocked on founder input item 1, "primary market-data provider",
and the capability register is right to carry `licensed_point_in_time_source` as an external
dependency rather than something engineering can close on its own.

Until then the audit does the one useful thing available: it makes the gap loud instead of
silent. A study that runs anyway now says so in its evidence record.
