# Cadence tick cutoff repair

## Scope and ownership

This is a narrow follow-up to the owner's 17 September 14:12 IST Lightsail
observation: all five latest decisions had `Future Market Data`, while health
reported fresh quotes. It does not implement the separate Item 6 regime/cache
follow-up or rebuild the existing Decision quality screen.

Only `marketdata/ticker_stream.py`, `orchestration/cadence.py`, two new test modules
and this document change. Pipeline, daemon, execution, risk, shared configuration,
Compose, CI, journal/outcome logic and active agents' working copies stay untouched.
Paper only. No real provider/model call, order, credential edit, deployment or
restart is performed. This PR must not be merged or deployed automatically.

## Reproduction, not an inference from a healthy container

The unchanged async pipeline fetches evidence and awaits headline scoring before
asking `CadenceMarketReader` for a tick against the original analysis time. The
old reader inspected only `TickBuffer.latest(symbol)`. A normal newer accepted
arrival could therefore replace an otherwise eligible tick and be rejected as
future relative to that older analysis time.

Two new tests fail on unchanged main at `13bbdec81fe59332513260b746d51ec75bcf565d`:
`test_newer_arrival_must_not_make_the_eligible_cutoff_tick_future` and
`test_tick_arriving_while_headlines_await_keeps_original_cutoff`.
Both produce the same `Future Market Data` refusal. The async test coordinates
an actual coroutine arrival while the scorer waits, without wall-clock sleeps.
The fake consensus transport is not called on the old path.

This establishes a reproducible defect matching the observed refusal. It is not
a capture of both timestamps from the original live failing request, and does
not prove every historical refusal on the host had this cause.

## Correction

`TickBuffer.latest_at_or_before(symbol, cutoff)` selects the newest retained,
accepted observation for that symbol whose event timestamp is at or before the
cutoff. It shares the existing buffer lock, keeps latest-arrival ordering for
same-timestamp revisions, and searches only the existing bounded deque. A
per-symbol latest entry that is already eligible remains available even if other
symbols have displaced it from that deque.

`CadenceMarketReader` uses this lookup only when the newest tick is later than
the supplied cutoff. It revalidates the selected result's identity, numeric
values, timestamp and existing age limit. It does NOT advance the cutoff,
rewrite the tick timestamp, allow a positive future tolerance, alter ingestion
validation, or widen the existing two-minute reader age budget.

Missing/evicted pre-cutoff history remains refused. A generic reader without
this optional history API still refuses a future tick. Invalid latest values
cannot be laundered by choosing an older valid observation. Custom selectors
are not trusted to implement the validation policy themselves.

The newest tick remains the newest tick in `latest()` for other consumers.
No stream event is dropped, rewound or rewritten by this read operation.

## Observable timing

When a post-cutoff observation triggers selection, an INFO event named
`cadence_tick_cutoff` records symbol, cutoff, newest observation time, selected
observation time (or unavailable), wall-clock check time and eligibility/refusal.
It records no prices, account identifiers, model prompts or credentials.
`result=eligible` means only this reader's checks passed, not that the model
recommended an order or that downstream risk/execution accepted it.

After owner-reviewed merge/deployment, from the host repository root:

```sh
docker compose --env-file .env -f deploy/docker-compose.yml   logs --no-color --since 30m pramana-ghost | grep 'cadence_tick_cutoff'
```

Absence of this event alone proves nothing: the older image has no such event,
INFO may not be retained, or a tick may already have been within the cutoff.
Check running revision first and correlate decision modes/reasons separately.
No existing journal is rewritten. A process created for inspection cannot read
the daemon's in-memory buffer; these events must originate in the daemon.

## Verification

The functional module includes cutoff/age boundaries, true future ingestion,
unknown symbols, eviction, same-timestamp revisions, mixed instruments/timezones,
custom selector failures, diagnostic output and unchanged latest-buffer state.
In the repaired async scenario the mocked consensus is reached once and uses the
original cutoff tick. Its response is explicitly NEUTRAL; no paper fill occurs.
This is not an attempt to force confidence or a trade through the risk checks.

The sabotage module runs each designated regression first against a disposable
unchanged source copy, then against exactly one deliberately modified boundary.
A passing control and a failing mutated test are required; collection errors and
skips do not count as detection. Source copies are restored and checkout hashes
must match. Full-suite and exact-head CI evidence are recorded in the PR.

The first local mutation runner had a quoting/collection error. After correcting
it, 16/18 ran successfully: one direct buffer-retention assertion was missing,
and one repeated guard anchor was ambiguous. The assertion and exact anchor were
corrected without changing production code; a lock-use check was then added.
The corrected campaign catches 19/19 cases. Initial results are retained rather
than represented as successful checks.

## Limits and outstanding host acceptance

Selection is bounded by retained event-time history, not a receipt-time snapshot
or a transaction over all news, fundamentals, daily bars and model inputs. No
external market authenticity or point-in-time availability is established.
If provider/model work outlasts buffer retention, the old observation may be gone;
refusal remains correct. Retention size is unchanged and not certified adequate
for every instrument count/tick rate/provider delay.

Age is checked against the requested analysis cutoff as before. This change does
not add a new execution-time price revalidation or change the other owners'
post-analysis risk/identity/protection controls. It does not close daily-history
recovery, regime warm-up, specialist attribution, token renewal, institutional
risk acceptance, genuine model evaluation or profitable forward-performance gates.

Still required: review and merge, deploy only approved clean main in the agreed
window, observe exact running revision and timing events, validate genuine
future/stale refusals with retained tests, and check actual model/fallback modes.
A model may still correctly abstain. Do not clear a halt, lower a floor, create
synthetic historical data or change an AWS/provider clock to force activity.
