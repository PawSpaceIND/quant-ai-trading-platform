# SaaS evidence and admission controls — owner integration handoff

This branch extends `main` without changing PR #50, the active daemon, existing
paper ledgers, Cloudflare routes, authentication, provider adapters or order APIs.
It is **not a SaaS launch or live-trading approval**. PR #53 owns the earlier
research panel handoff; PR #50 owns authenticated UI, risk controls and deployment.

## Added and executable

| Module | Behavior | Remaining boundary |
| --- | --- | --- |
| `quant_ai.research.calibration` | Brier score against a frozen constant baseline on paired resolved forecasts; reliability bins; full scheduled-case coverage including missing/errors/abstentions; fixed horizon/cost/version validation | Supplied evidence is not proof of forward collection; no automatic training, promotion, significance claim or portfolio performance claim |
| `validate_temporal_split` | Rejects future training, overlapping label windows and an insufficient predeclared embargo | Caller must supply full information/outcome windows; point-in-time corrections and undisclosed tuning are not detectable here |
| `assess_forecast_input` | Explicit abstention for absent probability, weak probability, provider failure, stale/future quotes, missing/future evidence, unverified corporate actions | Calibrated probability is not generic LLM confidence; never authorizes orders or replaces portfolio risk |
| `quant_ai.service.saas_controls` | Separate durable SQLite account state, per-tenant/per-source/per-use reviewed data grants, revocation/expiry, atomic monthly AI cost admission, idempotency, settlements and audit history | Trusted internal API, not authentication, subscription billing, encrypted credentials or a customer endpoint |
| `python -m quant_ai.research.saas_evidence` | Local read-only usage report and forecast calibration report | No public route; authenticated owner must scope any eventual browser projection |

## Data permission boundary

Every account starts absent/inactive and every source/use starts ungranted.
Supported purposes: `private_research`, `simulation`, `customer_display`, `model_input`.
One grant does not imply another. A market-data API login does not create a licence.
Only a trusted operator may record a grant after reviewing actual usage rights.
`evidence_sha256` points to the private reviewed agreement; it is not automatic legal
validation or a digital signature. Set expiry to the agreement's real end, not a
fictional indefinite date. Customer traffic must not call `grant_data` or
`configure_account`. Revoked/expired grants deny subsequent admissions.

Zerodha's published terms explicitly restrict public live-data display and use for
virtual/mock trading apps. Obtain written confirmation appropriate to the actual
product, or a suitably licensed data source, before commercial/paper expansion.
There are **no automatically approved Zerodha, NSE RSS or other provider grants**.
This branch does not claim to retroactively enforce rights on existing routes.

References checked 2026-09-14:
- https://kite.trade/terms/ — especially permitted access and end-user provisions.
- https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html
- https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf — selection bias from repeated strategy trials.

## Owner wiring contract

1. Place the separate `saas-controls.sqlite` in a private persistent directory with
   owner-only access. Include it in backups. Do not open a trading/ResearchLab DB
   with this class; a different table set is rejected. SQLite local audit rows are
   not tamper-proof; export to an independently controlled audit store for production.
2. Resolve the tenant from a validated server session, **never** a browser-supplied
   tenant ID. Verify resource ownership before loading any packet, conversation,
   export or broker credential. Library SQL uses tenant-qualified keys, but it
   cannot authenticate the calling tenant. Keep operator APIs private.
3. A verified, idempotent billing webhook may update account state; this branch
   does not integrate a payment provider or create paid subscriptions. A plan name
   alone is insufficient. Derive budget and model/source permissions server-side.
4. For market display, call `admission` for all actual sources and
   `purpose="customer_display"` before returning records. For simulation use
   `purpose="simulation"`. Apply to exports too. Identify licensed derived data
   requirements in the agreement; do not assume derived output is unrestricted.
5. Before a provider call, call `reserve` with the authenticated tenant, a stable
   request ID, a digest covering the exact packet/prompt/settings, a pinned model
   and a server-calculated maximum cost in integer micro-USD. Include every source
   sent to the model and verify additional model-provider data/privacy conditions.
   Set provider output limits and a versioned price schedule; handle unknown pricing
   by denying admission upstream. Never accept a customer-declared maximum cost.
6. Call the provider **only when `execute` is true**. Disable automatic retries in
   the provider client unless each attempt has a separate accounted reservation.
   Persist raw receipts privately. Timeouts, crashes or unknown cost leave the hold
   unresolved. The same request ID never authorizes another call, even next month.
7. Settle from a verified receipt using conservative integer rounding. Unknown cost
   is `None`, never zero. Zero is allowed only with evidence of no billed usage.
   Conflicting settlement is rejected; actual cost above the reservation is recorded
   honestly and pauses the account pending operator review of rate/token bounds.
   Admission and the upstream call are not one transaction; licence/account revocation
   cannot cancel a call already admitted. Keep the admission-to-call interval minimal.
8. Monthly allocation uses the trusted server's UTC request time. Pending holds remain
   in their original month. Settle late receipts against that original reservation;
   never move usage to hide overspend. This is usage admission, not invoice accounting.
   Reconcile provider invoices separately. Unknown holds require manual reconciliation,
   not time-based release. Restore DB before restarting provider traffic after a crash.
9. Use `assess_forecast_input` only where the probability has the declared meaning.
   Missing probability means abstain, not an invented 0.9. A passed assessment still
   needs PR #50's deterministic risk, broker scope and market-session checks.
10. Integrate sanitized reports into the existing authenticated Research/Admin views.
    No customer self-service tenant selector; no raw packets, secrets or receipt paths
    in Cloudflare snapshots. Add browser-to-API tenant isolation tests before release.

## Controlled improvement protocol

Freeze protocol ID, candidate/prompt/model version, cases, costs, horizon, baseline
and abstention threshold **before** evaluating outcomes. The positive label means
net return > 0 at the specified horizon under the stated cost policy. Outcomes must
carry the exact horizon and when they became available. Include every scheduled
case; unresolved outcomes do not count as wins or losses.

The calibration report is a diagnostic of probabilities, not a trading backtest.
It must accompany PR #51/52 continuous portfolio results, execution-cost stress,
all attempted candidate versions, regime coverage and forward observations.
Do not retune on a holdout then continue calling it untouched. Keep new candidates
in shadow evaluation and require separate evidence review before any promotion.
This module does not implement multiple-testing correction; never present its
Brier comparison as statistically significant superiority.

Input JSON format for `calibration`:

```json
{
  "protocol": {
    "protocol_id": "synthetic-smoke-only",
    "candidate_version": "model-prompt-v1",
    "cost_policy_id": "declared-fees-v1",
    "outcome_definition": "net_return_positive",
    "horizon_seconds": 3600,
    "baseline_probability": 0.5,
    "abstain_below_probability": 0.8,
    "scheduled_case_ids": ["case-1"],
    "frozen_at": "2026-09-14T09:00:00Z"
  },
  "records": [{
    "case_id": "case-1", "candidate_version": "model-prompt-v1",
    "status": "forecast", "probability": 0.8,
    "decided_at": "2026-09-14T10:00:00Z",
    "input_available_at": "2026-09-14T09:59:00Z",
    "outcome": {
      "net_return": 0.01,
      "measured_at": "2026-09-14T11:00:00Z",
      "available_at": "2026-09-14T11:01:00Z",
      "cost_policy_id": "declared-fees-v1"
    }
  }]
}
```

Run from this checkout with the intended environment:

```bash
PYTHONPATH=src python -m quant_ai.research.saas_evidence calibration --input /private/path/forecasts.json --as-of 2026-09-14T15:00:00Z
PYTHONPATH=src python -m quant_ai.research.saas_evidence usage --database /private/path/saas-controls.sqlite --tenant approved-tenant --as-of 2026-09-14T15:00:00Z
```

Reports print JSON to the operator's terminal. Usage opens SQLite read-only and does
not create a missing DB. Tests exercise both CLI paths using synthetic data; no
provider/API spend, customer account, model training or trading order is generated.

## Paid SaaS closure register

| Requirement | This branch | Owner/external work still needed |
| --- | --- | --- |
| Measurable model quality | Calibration, abstention and temporal split diagnostics | Immutable forward forecasts, justified labels, cost calibration, complete trial registry and strategy evidence |
| Portfolio protection | Reuses existing boundary; no duplicate order engine | Integrate and qualify PR #50 protections on the actual target host |
| Data rights | Deny-by-default reviewed-grant enforcement primitive | Broker/vendor written permissions and applicable RA/IA/algo-provider assessment |
| Tenant security | Durable tenant-scoped control data and tests | Identity provider, MFA, role rules, all-route ownership enforcement and independent penetration review |
| Credentials | No secrets accepted or stored | Per-customer broker consent, encrypted secret store, rotation/revocation and retention policy |
| Subscriptions | Active/inactive account and monthly usage admission | Select billing provider, verified webhooks, invoices/taxes, entitlement lifecycle, refunds and support |
| Availability | Restart-safe accounting | Always-on host, outside-host alerts, on-call ownership, restore drills and monitoring budget |
| Web/mobile UX | Existing owner interface preserved | Mount these reports, customer onboarding, mobile/PWA journeys and accessibility checks |
| Commercial proof | None claimed | Small customer pilot, retention, willingness to pay and per-customer gross-margin measurement |

Do not mark SaaS or live trading ready when this PR passes CI. Code, hosted
integration, commercial permissions, operational qualification and strategy
evidence are separate deliverables. PR #50 remains owned by the other Codex session.

## Verification at handoff

- New module and CLI tests: 41 passed; targeted Ruff check passed.
- Full branch Python suite on base main edeba3a: 391 passed.
- Temporary combined checkout with PR #50 at 206aa0e and PR #53 at 4da507e:
  conflict-free merges and 521 Python tests passed.
- This verifies source compatibility, not hosted authentication/billing integration.
- Two existing FastAPI/Starlette deprecation warnings; no failed tests.
- No actual provider request, paid subscription, permission grant or trade was created.
