# India paper operation

The local dashboard is http://localhost:3002. It monitors 13 NSE instruments
and dated Economic Times headlines. The independent paper engine evaluates
INFY, RELIANCE and TCS with 100,000 INR simulated starting capital.

## Runtime boundaries

- Run `.venv/bin/python scripts/india_paper_runtime.py` from the repository.
- Credentials are read from `~/.config/pramana/anthropic.key`,
  `zerodha.json` and `zerodha-session.json`; never commit them.
- Runtime ledger, proofs, heartbeat and logs live in
  `~/.config/pramana/india-paper/`. Tenant is `india-paper`.
- One launcher is allowed through a process lock. A restart preserves the ledger.
- This launcher refuses live-money configuration. It uses the existing paper broker.
- The cadence is ten minutes. Technical evidence requires sufficient fresh bars.
- Missing fundamentals/macro evidence remains unavailable and affected agents
  abstain. RSS sentiment is rule-based. Claude is the consensus provider, not
  five independently authenticated agents.
- Market closure permits monitoring/off-hours briefs, not paper order generation.
- MCX is not enabled in the verified account. Metal shares and gold/silver ETFs
  obey NSE sessions. No commodity-futures or US broker integration is enabled.
- The dashboard now declares the wider India research universe: NSE cash and
  index/ETF observations, MCX metals and energy, CDS currency derivatives, NSE
  derivatives and NCDEX agriculture. The latter groups are shown as planned
  until an exact broker contract is supplied.
- To add a read-only extra quote, set
  `PRAMANA_MARKET_EXTRA_INSTRUMENTS_JSON` to a JSON list containing the exact
  exchange symbol plus `contract` and `expiry` for MCX/CDS/NCDEX instruments.
  The collector rejects generic `GOLD` or `CRUDEOIL` labels, and these rows do
  not widen the pilot order gate. Contract expiry, lot size, tick size, product,
  session and broker entitlement must be reviewed before any paper execution
  change is considered.
- The Mac must remain awake and connected. This is a foreground/background local
  deployment, not a reboot-managed server installation.
- Kite access tokens are invalidated daily at about 06:00 IST and require renewed
  interactive authentication (`pramana zerodha-login`; see Daily routine). Saving a
  new token does not update an already-running engine: stop/restart the launcher
  after login. The quote collector reloads the saved session on every poll.
- A green process heartbeat proves the process is alive, not that every provider
  is connected. Check collector freshness, exchange timestamps and cadence errors.

## Daily routine

Kite invalidates every access token at about 06:00 IST, so each trading day:

1. After 06:00 IST and before the 09:15 IST open, run `pramana zerodha-login`
   (or `.venv/bin/python scripts/zerodha_login.py`) from the repository. It prints
   the Kite login URL; complete the login in a browser and paste the redirect URL
   (or its `request_token`) back. The command validates the new token against
   `kite.profile()`, writes `~/.config/pramana/zerodha-session.json` (mode 0600,
   `issued_at` recorded) and prints the next expected expiry. It never prints the
   token or the secret, and it refuses to run if `TRADING_LIVE_MONEY_ACTIVE` is set.
2. Restart the launcher: `.venv/bin/python scripts/india_paper_runtime.py`. It
   refuses a session issued before the most recent 06:00 IST cutoff with
   `Zerodha session expired at 06:00 IST; run: pramana zerodha-login` instead of
   connecting with a dead token. A session file without `issued_at` is refused the
   same way; re-create it with the login command.
3. Check status: `~/.config/pramana/india-paper/status.json` should show
   `"status": "running"` with a recent `updatedAt`, and its `providers` entry
   states which optional providers are configured. After the open, confirm on the
   dashboard that collector freshness and exchange timestamps advance. A `blocked`
   status carries the exception type; the launcher terminal shows the message.

## Start the dashboard against this ledger

From `apps/pramana-ui`:

```sh
PRAMANA_LEDGER_PATH="$HOME/.config/pramana/india-paper/ledger.sqlite" PRAMANA_PROOF_DIR="$HOME/.config/pramana/india-paper/proofs" PRAMANA_TENANT_ID=india-paper npm run start -- --port 3002
```

Run `.venv/bin/python scripts/market_monitor.py` in another terminal from the
repository for quotes and headlines. Do not run duplicate collectors.

## Validation on 14 September 2026

- 286 Python tests passed; Ruff passed; production UI build passed.
- Real Zerodha account/quotes and an actual structured Claude response verified.
- Monitor API: 13 instruments, 6 dated headlines; India CLOSED.
- Portfolio API: tenant india-paper, simulated equity 100000.
- No live orders sent. Empty intelligence proof on a closed session is expected.
- Open-session burn-in, licensed fundamentals/macro configuration and operational
  token renewal remain prerequisites for a broader unattended deployment.
