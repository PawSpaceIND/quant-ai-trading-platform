# Deployment Checklist

## Before any hosted paper environment
- Select cloud/runtime target.
- Provision isolated application, database, cache and object storage.
- Configure secret manager; do not use repository secrets as application storage.
- Configure licensed market-data provider credentials.
- Run migration/bootstrap and deterministic test suite.
- Enable logs, metrics, alerts and audit retention.
- Verify stale-data, broker-timeout, duplicate-order and kill-switch drills.

## Before shadow mode
- Connect broker sandbox/read-only account state where supported.
- Reconcile instrument identifiers and exchange calendars.
- Compare hypothetical fills against observed market/broker data.
- Run continuously across multiple market regimes.

## Before any live-money permission
- Complete jurisdiction/product compliance review.
- Use dedicated live credentials and least-privilege scopes.
- Require explicit live activation and human approval initially.
- Establish hard capital limits, daily loss limits and incident contacts.
- Confirm kill switch from an independent operational path.
- Complete paper/shadow promotion evidence.
