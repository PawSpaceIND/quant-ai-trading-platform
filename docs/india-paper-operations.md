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
- The Mac must remain awake and connected. This is a foreground/background local
  deployment, not a reboot-managed server installation.
- Kite access tokens require renewed interactive authentication. Saving a new
  token does not update an already-running engine: stop/restart the launcher
  after login. The quote collector reloads the saved session on every poll.
- A green process heartbeat proves the process is alive, not that every provider
  is connected. Check collector freshness, exchange timestamps and cadence errors.

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
