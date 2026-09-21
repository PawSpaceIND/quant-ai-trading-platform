# Operator dashboard acceptance matrix

What has been checked on the pilot dashboard, what it was checked with, and what is still
unaudited. This file is the record. A finding is closed only when it has a test that fails
without the fix, CI green on the merge commit, a deploy to the host, and a check on the
deployed page. Anything short of that is open, whatever the pull request says.

Why this file exists: the first audit found nine defects in code that had over three
hundred passing tests. The tests were real but aimed at `lib/`, the browser fixtures had
drifted from what the engine and the collector actually write, and no one had ever
enumerated the controls. Two of those three causes are addressed by keeping this list and
by generating fixtures from the producer rather than by hand.

## Closed

| Area | Finding | Fix | Evidence |
| --- | --- | --- | --- |
| Company events | "Watchlist only" never matched; mapping could not be recorded | Qualify the market row before comparing against a stored mapping | `tests/company-events.test.ts` watchlist and identity cases |
| Operator control | A refused halt showed nothing the operator could perceive | Report inside the dialog; the notice banner is inert and covered while it is open | `e2e/workspace-controls.spec.ts` refused halt, verified failing without the fix |
| Recovery | A refusal that never reached the gateway locked the panel permanently | Release the attempt for pre-dispatch refusals only; an unknown outcome still holds | `components/institutional-recovery.tsx`, pre-dispatch code list |
| Recovery | Duplicate submission guarded per browser tab only | No fix needed: the economic operation is convergent. The idempotency key is the durable slice transition `FILLED_UNACCOUNTED -> EXECUTED` inside the route lock | `tests/test_recovery_idempotency.py`, four cases incl. concurrency and restart |
| Exports | One anchor had no download attribute; blob exports clicked a detached anchor | Added the attribute; insert the anchor before clicking | `components/broker-observation.tsx`, `download()` |
| Exports | A failed export saved an error body under the report's own filename | Failures are named `-unavailable` | `tests/account-benchmark.test.ts` |
| Risk lab | "Downside to recorded stops" showed 0.00 when no stop existed | Withheld when no holding has a stop; a breached stop still contributes a real zero | `tests/portfolio-risk.test.ts` |
| Portfolio attribution | Sector weights excluded cash with no total to explain the gap | Cash is a line, any remainder is reported as unreconciled, and nothing is rescaled to reach 100% | `tests/portfolio-attribution.test.ts`, three cases |
| Post-mortem | Five of seven engine summary sections were silently dropped | Counts, rejections and regimes shown; exits and hourly results behind disclosures | `tests/decision-quality.test.ts` incl. a no-trade session |
| Proof evidence | File proofs carried no account and were shown as this account's decision | The account is written on the file; an unlabelled file is shown as ownership unverified and excluded from account decisions; ownership is recovered only through ledger linkage | `tests/test_proof_ownership.py`, `tests/pilot.test.ts` ownership cases |
| Proof evidence | The proof directory grew forever and was re-read on every poll | Size and record bounds in the reader; daily pruning that never removes a proof for a decision that produced an order | `tests/test_proof_ownership.py` pruning cases |
| Workspace freshness | Broker and external-account panels claimed a current reading beside an expired banner | Both re-aged with the snapshot | `tests/freshness.test.ts` |
| Workspace | Refresh and Retry were silent no-ops while a poll was outstanding | Both show that a refresh is running | `components/pilot-workspace.tsx` |
| Atlas chat | A republished pinned run comparison spent a daily question on a call that never ran | The pinned report resolves before the budget is consumed | `app/api/copilot/route.ts` |
| Overview | The daily-loss breaker was published by the engine and drawn nowhere, although breaching it engages the kill switch | A bar beside drawdown and gross exposure, computed with the daemon's own arithmetic | `tests/portfolio-risk.test.ts` daily-loss cases, `e2e/workspace-controls.spec.ts` |
| Risk lab | "Portfolio valuation status: ok" for a ledger-marked or replayed book, beside its own count of stale marks | The panel names the mark mode and says a non-engine status is the one the book was written with, not a currency check | `e2e/workspace-controls.spec.ts`, verified failing without the fix |
| Risk lab | Clearing a per-holding shock box wrote an explicit 0% override, dropping that holding from the scenario until Reset | An empty or unparseable box means no override; the holding follows the common shock | `tests/portfolio-risk.test.ts` edit-rule cases |

Note on tenancy: both containers running as the same tenant is a deployment fact and does
not establish ownership of a file. That is why the proof fix is at the write and read
layers and not an argument from the deployment.

## Checked and found sound

Recording these so they are not re-investigated as if they were open.

- `agePortfolio` treating a missing `holdings` key as an empty book is deliberate: older
  cash-only snapshots did not serialize the array, and an explicitly malformed value is
  still invalid and cannot claim a current mark.

## Note on browser tests and the sign-in limit

The sign-in route allows ten attempts per minute and the browser suite runs as one worker
against one server, so specs that drive the login form spend that budget and a new one can
push an unrelated spec over it. `e2e/workspace-controls.spec.ts` shows the answer: mint the
session cookie with `makeSession()` and add it to the context. A browser test should drive
the form only when the login flow itself is what it is testing. Raising the limit for tests
would weaken a real control.

## Open, not yet audited

These have had no systematic pass. Listing them is not a claim that they are broken.

- The Atlas chat surface itself: conversation lifecycle, budget exhaustion states, errors.
- Login and authentication flows beyond session expiry and sign-out.
- Overview and Risk lab panels in depth.
- Notifications and alerts.
- The hosted Cloudflare worker build.
- Accessibility beyond roles and labels already used by tests.
- Viewports between 390px and 1280px.

## How a finding gets closed

1. A test that fails without the fix. For a browser-visible defect, a browser test.
2. Fixtures generated from the producer, never hand-written to match the code.
3. CI green on the merge commit.
4. Deployed to the host with `./scripts/deploy_pilot_host.sh`.
5. Checked on the deployed page, and the row above updated to say so.
