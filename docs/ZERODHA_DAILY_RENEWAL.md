# Daily Zerodha session renewal — paper pilot

## Scope and authority

This change prepares source and tests only. It does not merge, deploy, restart the
running host, read its secrets, or enable live-money orders. The owner must review,
merge and deploy the code before any of this changes the running pilot.

Kite's documented access-token cutoff is 06:00 the following day. The documented
profile endpoint is read-only and does not return tokens:
https://kite.trade/docs/connect/v3/user/#response-attributes
https://kite.trade/docs/connect/v3/user/#user-profile
The user's interactive login/2FA remains required. No credential is generated or
renewed without that interaction; this removes hand-editing and missed-session
silence, not authentication requirements.

## Design

The existing root `.env` remains the Docker source of truth. This is the explicit
atomic-updater option in the task, rather than mounting `~/.config/pramana` into a
container with a different UID. No API-secret directory is exposed to UID 10001,
and no session-file permission is relaxed from 0600.

`publish_to_env` validates the newly issued session against the API key actually in
that `.env`, preserves an existing account binding, and changes only these fields:

- `ZERODHA_ACCESS_TOKEN`
- `PRAMANA_ZERODHA_TOKEN_ISSUED_AT`
- `PRAMANA_ZERODHA_USER_ID`

The target must be a private, owner-controlled regular file, not a symlink or
multiply-linked file. Duplicate managed assignments, interpolated/malformed scalar
credentials, expired/future sessions and account changes refuse. A cooperating
private lock, same-directory 0600 temporary, fsync, complete-byte readback,
concurrent-edit check and atomic rename keep the old environment visible until the
new candidate is complete. No persistent secret backup is created. An unrelated
writer which ignores the lock is not a fully coordinated transaction participant;
do not run deployment/environment editors concurrently with renewal.

The real environment-based daemon boot path validates `kite.profile()` before
assembling broker/stream state. Legacy tokens without issuance metadata still need
this authoritative profile check. Rejected/unavailable/missing credentials, bad
issuance metadata and account mismatch refuse startup and dispatch a CRITICAL
`ZERODHA_SESSION_INVALID` alert to the existing durable alert mechanism.

A refusal and an unanswered question are reported apart, because the operator's next
action differs. `zerodha_profile_rejected` means the provider answered and would not
accept these credentials: a new interactive login is required. `zerodha_profile_unavailable`
means the call never got an answer, so the stored token may be perfectly good and the
alert says so rather than sending someone to re-enter working credentials. The provider's
own exception is classified by type name and then dropped; it is never stored, chained or
rendered, since its message carries request context and sometimes the credential.

Only the unanswered case is retried, at most `PROFILE_ATTEMPTS` times with a short
bounded backoff, and only on the runtime boot and watch paths. On 22 September 2026 the
daemon exited twice on a transport failure in the first second of a freshly created
container - 02:09:29 and 02:15:39 - each time dispatching the same CRITICAL alert that a
stolen token would, and each time running a minute later on the identical token. An alert
an operator must not learn to ignore fired twice for nothing. A refusal is never retried:
repeating a rejected token cannot change the answer and would only delay the alert. The
interactive helper still asks exactly once, because an operator is watching it.

All provider exception details are discarded at the credential boundary, rather
than guessing which substrings to redact. Session/token dataclass representations
hide access tokens. The owner helper uses a hidden terminal prompt for the request
token; it refuses non-TTY input rather than falling back to visible echo.

## Owner deployment and daily operation

1. Review and merge the PR; deploy with the existing `./scripts/deploy_pilot_host.sh`
   during the normal safe deployment window. This introduces the `token-watch`
   service. No deployment has been performed as part of this source change.
2. Use the Python environment already used for interactive Kite login, with this
   project and its `[pilot]` dependencies installed. Run from the deployed main
   checkout `/home/ubuntu/quant-ai/quant-ai-trading-platform`. Its `.env` is at the
   repository root and must be mode 0600. The existing credentials file remains
   `~/.config/pramana/zerodha.json`, also mode 0600.
3. After the daily cutoff and before the open, run:

```bash
python scripts/renew_pilot_token.py --restart
```

Complete the normal browser login and 2FA, then paste the redirect/request token
into the hidden prompt. The helper validates it, publishes the environment
atomically, and recreates only `pramana-ghost`, `market-monitor`, and `token-watch`.
It does not pull, build, replace the dashboard, or run the deployment script.
The helper checks main/clean-tree/session permission before login, after login,
and immediately before container refresh. `--force` is an explicit acceptance of
mid-session candle loss, not the default. A failed/aborted step is not proof that
all consumers refreshed: inspect their health and the alert log.

Without `--restart`, publication alone intentionally leaves running containers on
their prior environment. The command says so. The low-level publisher is also
available for an already-created session via
`python -m quant_ai.operations.zerodha_renewal publish --env-file .env`.
Never display `.env`, the session file, `docker inspect` environment output, or
unredacted `docker compose config` when checking the result.

## Independent pre-open alert

`token-watch` uses the shared alert volume and has no dependency on a healthy or
running ghost daemon. It checks once at its own start and every five minutes in
08:30–09:14 IST. It deliberately checks every calendar day, including holidays;
it does not guess the exchange calendar. Invalid sessions emit CRITICAL alerts
while the watcher survives to check again. A small heartbeat lets Docker identify
an unresponsive watcher. Watcher liveness is not token validity.

Alerts are written to `/data/alerts.jsonl`. Telegram delivery is added only when
both `PRAMANA_TELEGRAM_BOT_TOKEN` and `PRAMANA_TELEGRAM_CHAT_ID` are configured.
Durable file/console alerts do not establish that a person saw the alert. Actual
push delivery, permissions, container recreation and the before-open host test
remain owner acceptance gates; they have not been observed on Lightsail here.

## Verification evidence

The PR body and `docs/evidence/zerodha-token-renewal-sabotage.json` contain the exact
executed mutation/test pairs and restored source hashes. Mutants run with a
separate, non-writing bytecode-cache prefix so identical-size edits cannot reuse
stale Python bytecode. A mutation counts only if the selected executable test
fails with no collection error. All altered source is restored before the full
suite. Tests use clearly synthetic credentials and fake read-only profiles; no
real Kite session or order is used.

Baseline `7fb90bbbbb59bcb3ec96975a09848d78420bb5cf` on the connected Mac:
1536 passed, 13 existing deployment-script portability failures, 2 warnings,
14 subtests passed. Final candidate: **1585 passed, the identical 13 failures**, 2 warnings, 14 subtests
passed. There are 49 additional passing cases and 32 caught guard-removal mutations.
All 403 source/test/runtime files stayed unchanged during final certification; the
exact JUnit comparison is retained in `docs/evidence/zerodha-token-renewal-suite.json`. `/usr/local/bin/ruff` is absent on this Mac;
lint is executed with the absolute project virtual-environment interpreter and
Ruff 0.16.7, not a shadowed PATH executable. Linux CI is separately reported.

## Still refusing / not established

No automatic 2FA, guessed access token, wrong-account fallback, live-order route,
statutory rate or margin assumption is introduced. Provider failure refuses even
when it might be transient. No running-host token renewal, Telegram receipt,
new-sidecar runtime observation or full market session is claimed by unit tests.
