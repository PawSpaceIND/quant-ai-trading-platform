# Zerodha owner refresh: preserve the published session at the Compose boundary

Follow-up to the Item 1 implementation at `916ff1681cacf1395476dfdbf41378eddd5a6bb0`.
This is an independent correctness repair, not a resolution of the earlier Gitleaks finding.

## Reproduced defects

Docker Compose gives shell variables precedence over an explicit `--env-file`.
The owner helper inherited those variables unchanged. A successful atomic publication
could therefore recreate all consumers with an exported old or empty access token,
a different API key, or stale issuance/account metadata.

A relative `--env-file` was also validated and published relative to the caller,
then interpreted by Docker after changing its working directory to the checkout.
The same command could therefore publish one file but recreate from another file.

Authority for Compose precedence:
https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/

## Repair

`refresh_services` makes the selected environment path absolute before changing
subprocess working directory. It deliberately does not resolve symlinks or relax
any private-file checks.

The Docker child receives a copy of the parent's environment without
`ZERODHA_API_KEY`, `ZERODHA_ACCESS_TOKEN`, `PRAMANA_ZERODHA_TOKEN_ISSUED_AT`, and
`PRAMANA_ZERODHA_USER_ID`. Those fields now come from the selected file. Unrelated
shell settings and the parent's environment are preserved. Main, clean-tree and
market-session refusal gates, private publication, hidden prompt, no-build behavior
and the three-service allowlist remain unchanged.

## Executed verification and transport limitation

Before repair, the initial regressions reported 9 failed / 1 passed. After repair,
all 11 new cases and the combined 119-case renewal/login/daemon/health set passed.
Ruff `src tests scripts/renew_pilot_token.py` and whitespace checks passed.
Four independent mutations were detected by assertion failures with no collection
errors. The source was restored and all 11 new cases passed again. Exact guard/test
pairs are in `docs/evidence/zerodha-renewal-compose-boundary.json`.

A fresh full Mac baseline completed: 1585 passed, 13 existing deployment-portability
failures, 2 warnings and 14 subtests passed. The candidate full run started, but the
Mac stopped responding before its result could be retrieved. That result is UNKNOWN,
not a passing claim. The connected GitHub API was used to publish this same tested
source/test change to the existing draft PR; exact-head Linux CI is a separate gate.
The original `docs/ZERODHA_DAILY_RENEWAL.md` and its original evidence are retained.

All tests use synthetic inputs and a substituted Docker boundary. No real Docker
refresh, real Kite login/profile request, deployment, production environment change,
merge, live-money action, or scanner exception was performed. Actual host renewal,
alert delivery and target-host acceptance remain open. Items 2-4 are not advanced.
