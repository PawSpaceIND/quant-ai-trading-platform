# Gate 2 — Live Paper Burn-in Runbook

Real market ticks in, paper fills out. This runbook launches the ghost daemon and the
dashboard on one shared ledger, under the founder's directives, with the operator
controls that exist today.

## What is real and what is not

| Input | Source in the ghost daemon | Status |
|---|---|---|
| Prices, candles, marks | Zerodha / IBKR websocket ticks → `LiveTickMarketDataFeed` | **Real**, any market the streams carry |
| Protective stops / targets | Persisted on fill, swept at the top of every cadence tick against the live tick | **Real** (latency ≤ cadence, 10 min) |
| Equity, unrealized P&L, drawdown, peak | Marked from the live tick; peak persisted in `paper_accounts.peak_equity` | **Real** |
| News sentiment | `PRAMANA_NEWS_RSS_URLS` (keyword sentiment) | Real if set, else sandbox constants |
| Macro (US10Y, INDIA10Y, BRENT, GOLD, DXY) | `FRED_API_KEY` | Real if set, else sandbox constants |
| Fundamentals (trailing P/E, debt/equity, operating margin, FCF yield) | `PRAMANA_FUNDAMENTALS_PROVIDER=yahoo` (default): Yahoo Finance `quoteSummary`, no key, cached 6 h per symbol | Real when Yahoo returns all four ratios for a watchlist/target symbol; otherwise valuation agents abstain. `none` disables |
| Consensus | Five specialist agents → Atlas; LLM refinement with `ANTHROPIC_API_KEY` | Real |
| Execution | Local paper ledger only; no live order code path exists | Paper, by design |

A burn-in with news and macro left on sandbox tests the price path, sizing, stops and
governance faithfully; it does **not** test the intelligence layer's judgement.

## Prerequisites

- Python ≥ 3.9 (CI runs 3.12), Node ≥ 22 for the dashboard (`node:sqlite`).
- Zerodha Kite credentials and the instrument tokens you are licensed to consume.
  Kite carries NSE/BSE equities and indices, NFO, MCX metals and commodities, and
  CDS currency pairs; the daemon subscribes to whatever tokens you list.
- US instruments need IB Gateway/TWS reachable and `PRAMANA_IBKR_ENABLED=true`.
- `ANTHROPIC_API_KEY` for LLM consensus (without it Atlas runs deterministically).

## One-time setup

```bash
git checkout main && git pull
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' kiteconnect ib_async      # ib_async is imported at boot even if IBKR is off
cp .env.example .env && chmod 600 .env
cp deploy/founder-directives.example.json founder-directives.json
rm -f pramana_ledger.sqlite*                      # start the burn-in on a fresh ledger
```

Edit `.env`: credentials, `PRAMANA_ZERODHA_TOKENS_JSON` / `PRAMANA_ZERODHA_SYMBOLS_JSON`
(every watchlist symbol must appear as a mapped symbol — the daemon logs a warning for
any that do not), `PRAMANA_FOUNDER_DIRECTIVES_FILE=./founder-directives.json`, and
optionally `PRAMANA_NEWS_RSS_URLS`, `FRED_API_KEY`, `PRAMANA_TELEGRAM_BOT_TOKEN` /
`PRAMANA_TELEGRAM_CHAT_ID`.

Edit `founder-directives.json`: capital, risk mode, allowed markets and asset classes,
the watchlist, the position cap, and your instructions. Instructions reach the Atlas
consensus prompt and are stamped on every XAI proof; they can narrow what the swarm
does, never widen a firewall limit.

## Launch

The daemon does not read `.env` by itself; export it into the shell.

```bash
# terminal 1 — ghost daemon (live ticks → paper ledger)
cd /path/to/quant-ai-trading-platform && source .venv/bin/activate
set -a; source .env; set +a
export TRADING_LIVE_MONEY_ACTIVE=false PRAMANA_TENANT_ID=ghost PRAMANA_GHOST_LOG="$PWD/pramana-ghost.log"
python -m quant_ai.daemon

# terminal 2 — dashboard
cd /path/to/quant-ai-trading-platform/apps/pramana-ui && npm install
PRAMANA_TENANT_ID=ghost npm run dev            # http://localhost:3000
```

`PRAMANA_TENANT_ID` must match on both sides (the daemon defaults to `ghost`, the UI to
`default`). Ledger and proofs resolve to the repository root on both sides with no
further configuration.

## Operator controls

```bash
pramana halt --reason "founder review"   # kill switch engages on the next cadence tick
pramana resume                           # released on the next tick
tail -f pramana-ghost.log                # JSON lines: ticks, exits, faults, proofs
```

A halt freezes new risk only: protective exits keep running. A halt latched by
repeated cadence failures (`cadence_halted` in the log) is not released by `resume`;
fix the cause and restart the daemon.

Holidays: NYSE 2026 closures are built in. For NSE only civil-calendar closures are
built in; load the lunar-calendar dates from the exchange circular:

```bash
export PRAMANA_HOLIDAYS_JSON='{"INDIA": ["2026-03-26", "2026-03-31", "2026-11-09"]}'
```

AI spend: Anthropic calls are capped per UTC day on both paths. The daemon admits at
most `PRAMANA_AI_DAILY_CALL_LIMIT` (500) consensus calls and
`PRAMANA_AI_DAILY_TOKEN_LIMIT` (2,000,000) tokens, counted in `ai-budget.sqlite` next
to the ledger (`PRAMANA_AI_BUDGET_DB`). Once either is reached the consensus degrades
to NEUTRAL and the tick ends in PRESERVE_CAPITAL; the proof shows
`Consensus Skipped: AI budget exhausted` with inference status `budget_exhausted`, and
the log carries one `anthropic_consensus_budget_exhausted` warning per day. A budget
file the daemon cannot read refuses the call the same way. The dashboard admits
`PRAMANA_CHAT_DAILY_LIMIT` (200) Atlas chat calls per day and answers 429 afterwards,
with a `copilot.budget_exhausted` audit row; `GET /api/copilot` reports
`dailyRemaining`. Counters reset at 00:00 UTC; a value of 0 or less disables that cap.

## What to watch (first 24–48h of session hours)

**Data path** — a proof every 10 minutes in `pramana-proofs/`; `price=FRESH` in the
provider status. `Missing Market Data` / `Stale Market Data` as the veto reason means
the token→symbol map does not match the watchlist. Outside session hours the mode is
`OFF_HOURS_MACRO_GEO_SWEEP`. `insufficient_price_history` on the technical agent is
normal for the first 50 minutes after the stream connects.

**Sizing and scope** — first fill notional ≈ 5% of equity; size moves with equity,
never a constant. `position_already_open` (C2), `re_entry_cooldown_active`,
`max_open_positions_reached` and `asset_class_blocked` are governance working, not
faults. `position_sizer_no_capacity` means no risk budget or deployable capital
remained — check the plan before assuming a bug.

**Stops** — the exit sweep runs before analysis on every tick, so a stop fills at the
next tick's mark (expect slippage past the threshold on a fast move). Look for
`protective_exit` events and a SELL row with EXACT PROOF. A run of
`protective_exit_mark_skipped` warnings during session hours means the tick feed is
silent and **stops are not being enforced for the gap** — treat it as a feed outage
(Zerodha access tokens expire daily).

**Halts** — after a breach, BUYs show `max_drawdown_reached` /
`daily_loss_limit_reached`; SELLs show `approved_risk_reducing`. The drawdown tile
tracks the persisted peak. Kill and restart the daemon once mid-session: peak,
positions and cooldowns must survive; no duplicate fill on the next tick.

**Proofs** — every trade row shows EXACT PROOF; every proof carries
`founder_directives=…` when instructions are set.

## Known limits of this build

- Fundamentals come from Yahoo Finance's public `quoteSummary` endpoint (crumb-and-cookie
  session, no API key, no data licence). The provider returns all four ratios or nothing:
  valuation agents abstain when Yahoo omits a field (trailing P/E is absent for loss-making
  companies), when the endpoint changes shape or rate-limits, or when the symbol is outside
  the watchlist/target. Snapshots are cached six hours per symbol, so a new filing reaches
  the engine up to six hours late. A licensed fundamentals provider remains a founder
  decision.
- RSS sentiment is keyword-based. It is real data, not a strong signal.
- Exposure is keyed by symbol; the directives reject duplicate symbols across markets
  for that reason.
- The sandbox CLI runtime (`pramana run-once` / `pramana daemon`) is US-only and
  synthetic; use `python -m quant_ai.daemon` for live ticks.
- Live-money promotion still requires the evidence gates in `LIVE_MONEY_GATES.md`.
