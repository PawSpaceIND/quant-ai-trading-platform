# Session-aware persisted health observation

Ownership: issue #159. Reviewed base: bf3812088e4308eb5372203669d9e07cd8e74562.
Only operations/health.py changes production behavior. No session definition,
holiday date, daemon, telemetry writer, trading gate, risk limit, or deployment
configuration is changed. This is observation, not trading permission.

## Correction

The old observer constructed MarketCalendar without the configured holidays and
ignored exchange identity. It also required every declared market to be open
before identifying a fully stale feed. Thus a closed market could hide a blind
open market, while a normal holiday could generate a false feed alarm.

The reader now reuses default_holidays and holidays_from_json, matching the
existing daemon/monitor interpretation of PRAMANA_HOLIDAYS_JSON. It resolves each
entry against the existing exchange session. Only entries currently in regular
hours contribute to the open subset's fresh/stale classification. No historical
session data, exchange hours, or default holiday constants are invented here.

## Added market_data observation

| state | Interpretation |
|---|---|
| fresh | Every declared open entry reports fresh=true. |
| partial | Some, but not all, declared open entries report fresh=true. |
| blind | At least one entry is open and all open entries report fresh=false. |
| closed | All resolvable declared entries are outside regular hours. |
| unknown | Calendar or watchlist cannot be safely interpreted. |

The object contains bounded open/fresh counts and fixed reason labels, not symbols,
account values, submitted settings, or arbitrary exceptions. The current 64-entry
watchlist and payload bounds are preserved. Unresolvable GLOBAL/unknown markets
and malformed entries produce unknown, not an exception or a fresh claim.

A blind open subset adds market_data_stale_during_session to the existing health
reasons. Invalid or oversized holiday configuration adds market_calendar_invalid.
A partial feed does not cause a whole-feed alarm. A valid heartbeat with an
unreadable watchlist retains the prior heartbeat-only availability behavior but
now explicitly reports market_data.state=unknown. Do not interpret observation_ok
as verification of all required inputs, quote freshness, or execution readiness.

## Verification and limits

Tests use actual SQLite state and the existing pilot_ops health command, plus the
current session definitions and synthetic observations. They include defaults,
overrides, exchange-specific closure, mixed open/closed venues, weekends, the
existing special session, evening exchange intervals, bounds, redaction, persisted
halts and source-state preservation. Existing tests are unchanged.

Eight deliberate-removal cases reuse the existing offline copied-source harness.
Each needs a passing control and named failing assertions with no errors/skips;
source copies restore and protected hashes match. Inventory and measured outcomes
are in docs/evidence/session-health-guards.json and session-health-verification.json.
Completed exact-commit CI/full-suite results are recorded in the PR.

This reader trusts a fresh heartbeat's declared fresh flags. It does not fetch or
independently authenticate quotes. A process environment does not prove that the
running daemon uses the same configuration. Custom runtime-only calendars and
unknown exchanges are not qualified by this environment-based observer.

A health failure must not automatically restart protection or clear a halt. This
patch performs no recovery, order, provider request, token action or service
restart. The paper-only/live-money restrictions and freshness TTLs are unchanged.
Actual AWS operation, quote completeness, independent alert receipt and restore
qualification remain external acceptance requirements.

## Ownership and release

The production path was checked against non-ancestor remote branch changes and
claimed in #159; exchange work was notified on #151. No peer health.py delta was
present at resumption. Notifications/trading.py was deliberately left alone
because learning branches already modify it. AI #153, boundary #156 and all
execution/risk/recovery/learning/input/watchlist/MCX owners remain separate.

Review and merge this isolated correction only after exact-source checks pass.
A Git merge does not update AWS containers; target-host deployment and acceptance
must explicitly bind the reviewed release. This is not live-money authorization
or proof of investment effectiveness.

Earlier retained failures: a quoted-JSON mistake stopped collection of the first
new test; it was corrected before the recorded baseline. A combined remote
operation in the earlier turn was rejected before execution. On resumption, the
new CLI test was separately strengthened with check=True while retaining its
return-code assertion; no production safety check was bypassed or disabled.
