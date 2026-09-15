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
- Zerodha Kite credentials (`api_key` and `api_secret` in `~/.config/pramana/zerodha.json`,
  mode 0600) and the instrument tokens you are licensed to consume. Kite issues one
  access token per interactive login and invalidates it every day at about 06:00 IST;
  `pramana zerodha-login` performs that login and records when the token was issued.
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

Every trading day, after 06:00 IST and before the 09:15 IST open, renew the Kite token
first. Yesterday's token is invalid after the cutoff, and an invalid token does not
error: the stream goes quiet and stops are not enforced for the gap.

```bash
pramana zerodha-login          # prints the login URL; paste the redirect URL back
```

The command exchanges the request token for an access token, checks that `kite.profile()`
reports the same `user_id` as the new session, writes `~/.config/pramana/zerodha-session.json`
(mode 0600, with `issued_at`) and prints the next expected expiry. It never prints the
token or the secret. `scripts/india_paper_runtime.py` reads that file directly and refuses
to start once the token is past the cutoff. The generic daemon below reads
`ZERODHA_ACCESS_TOKEN` from the shell, so export it from the session file after `.env`.

The daemon does not read `.env` by itself; export it into the shell.

```bash
# terminal 1 — ghost daemon (live ticks → paper ledger)
cd /path/to/quant-ai-trading-platform && source .venv/bin/activate
set -a; source .env; set +a
export ZERODHA_ACCESS_TOKEN="$(python -c 'import json, pathlib; print(json.load((pathlib.Path.home() / ".config/pramana/zerodha-session.json").open())["access_token"])')"
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

A halt freezes new risk only: protective exits keep running.

`resume` releases operator halts and nothing else. It removes the halt marker file and
clears the persisted halt only when that halt came from the marker file. A halt the
engine latched itself - `portfolio_drawdown_limit`, `portfolio_daily_loss_limit`,
`paper_ledger_reconciliation_failed`, `paper_position_protection_incomplete`,
`protective_exit_failed`, `runtime_strategy_changed_or_unavailable`, a latched cadence
fault - is a finding, not a pause. `resume` prints the reason and exits 2 without
touching it.

Fix the cause first. If an operator then has to override the halt deliberately:

```bash
pramana resume --clear-fault-halt --operator "NAME"
```

It prints exactly what it is clearing and appends the override to a hash-chained record
next to the ledger (`halt-overrides.jsonl`, or `PRAMANA_HALT_OVERRIDE_LOG`). The record
carries the halt reason, the tenant and the operator name, and each line pins the digest
of the line before it, so an override cannot be removed from the file unnoticed.

Holidays: NYSE 2026 closures are built in. For NSE only civil-calendar closures are
built in; load the lunar-calendar dates from the exchange circular:

```bash
export PRAMANA_HOLIDAYS_JSON='{"INDIA": ["2026-03-26", "2026-03-31", "2026-11-09"]}'
```

## Scheduled-event blackouts

Some days are known in advance to be bad days to open a position: the morning a company
reports, the day a central bank decides, budget day. The engine has no earnings feed and
no policy calendar, so it does not guess those dates — you write them down and it obeys
them. Point `PRAMANA_EVENT_CALENDAR` at a JSON file:

```json
{
  "timezone": "Asia/Kolkata",
  "events": [
    {"date": "2026-10-15", "category": "earnings", "symbol": "INFY", "note": "Q2 results"},
    {"date": "2026-10-01", "category": "rbi_policy"},
    {"date": "2026-02-01", "category": "budget_day"},
    {"date": "2026-03-17", "through": "2026-03-18", "category": "fed_decision"}
  ]
}
```

An event with a `symbol` blacks out that instrument; an event without one is index-level
and blacks out every instrument. `category` must be one of `earnings`, `rbi_policy`,
`fed_decision`, `budget_day`, `data_release`, and an `earnings` entry must name its
symbol. Dates are exchange-local calendar days, not UTC, and `through` makes a closed
multi-day range.

What it does, exactly: on a blackout day the pilot pre-submit path refuses **new
entries** in that instrument and the veto reason reads `event_blackout:earnings`. It
never forces, delays or blocks an exit — protective exits do not consult the calendar —
and it is not a halt, so the next day the same entry is allowed again. An unset variable
means no blackouts at all; a file that is set but missing, unreadable or invalid stops
the daemon at boot rather than letting it run believing it is honouring blackouts it
never loaded. Check the startup log for one `event_calendar_loaded` line with the event
count you expect.

## Cross-asset risk-off destination

A watchlist of three correlated Indian IT and energy names gives a risk-off signal
nowhere to go: cash is the only alternative to those three. `GOLDBEES` and `SILVERBEES`
are NSE cash ETFs — they trade like equity, settle in INR, and work unchanged with the
long-only paper ledger, the protective stops and the position sizer. The supplied
`deploy/founder-directives.example.json` watches `INFY`, `TCS`, `RELIANCE`, `GOLDBEES`
and `SILVERBEES` with `allowed_asset_classes: ["EQUITY", "ETF"]`; `asset_class` must be
`ETF` and `exchange` must be `NSE` for both.

Two things an ETF entry does not change and one it does. Pilot scope already accepts NSE
cash ETFs, so no gate has to be relaxed for them. The valuation agents treat an ETF as
equity-like but it carries no balance sheet, so fundamentals stay missing and those
agents abstain — expect ETF decisions to lean on the technical, macro and news agents.
And every ETF symbol still needs a websocket mapping.

**You must supply the instrument tokens.** None are written down here or anywhere else in
this repository, and a token copied from documentation is the wrong token. Pull your own
account's instrument dump from Kite (`https://api.kite.trade/instruments`, or
`kite.instruments("NSE")` from `kiteconnect`), filter to `exchange == "NSE"` and match the
exact `tradingsymbol`, then put the numeric `instrument_token` values into
`PRAMANA_ZERODHA_TOKENS_JSON` and the token-to-symbol map into
`PRAMANA_ZERODHA_SYMBOLS_JSON`. Every watchlist symbol must appear in the symbol map; the
daemon logs `watchlist symbol ... has no websocket mapping` at boot for any that do not,
and those names are vetoed at every tick as missing market data.

## Headline sentiment

Headlines are scored two ways. The deterministic scorer counts hopeful words against
frightening ones; it cannot read negation ("no war expected" reads as a war), cannot weigh
whether a story bears on the instrument at all, and it is what runs whenever the model
path is unavailable. With `ANTHROPIC_API_KEY` set, each headline is instead scored against
the specific instrument through the same structured tool-use discipline as the consensus,
with a one-line rationale.

Every proof records this. `headline_scorers` counts the headlines by scorer (`model` or
`keyword`) and `headlines` lists each one with its sentiment, its scorer and its
rationale, so you can always tell which number a decision used and why. The consensus
prompt carries the same `scorer=` and `rationale=` fields on each headline line.

Headlines remain untrusted third-party data on both paths: they are collapsed to one
bounded line inside a delimited data block, the system prompt states that text asking the
model to change its task is itself the datum to score, and a model rationale is bounded
and stripped of the separator so it cannot forge another evidence field.

Scoring degrades rather than failing. No key, a spent daily budget, a provider fault, a
timeout or a payload off the strict schema all fall back to the word counter for exactly
the headlines that were not scored, with the reason recorded as the rationale
(`keyword_fallback:budget_exhausted`, for example). Model scores are cached per headline
per instrument, so a story is not re-scored every ten-minute tick; a keyword fallback is
never cached, so the next tick retries once the budget or the provider recovers.

AI spend: Anthropic calls are capped per UTC day on both paths. The daemon admits at
most `PRAMANA_AI_DAILY_CALL_LIMIT` (500) consensus calls and
`PRAMANA_AI_DAILY_TOKEN_LIMIT` (2,000,000) tokens, counted in `ai-budget.sqlite` next
to the ledger (`PRAMANA_AI_BUDGET_DB`). Once either is reached the consensus degrades
to NEUTRAL and the tick ends in PRESERVE_CAPITAL; the proof shows
`Consensus Skipped: AI budget exhausted` with inference status `budget_exhausted`, and
the log carries one `anthropic_consensus_budget_exhausted` warning per day. A budget
file the daemon cannot read refuses the call the same way. Headline scoring counts
against the same limits under its own `headline_sentiment` scope, so a heavy news day
cannot quietly consume the consensus allowance and the two spends are readable apart. The dashboard admits
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
(Zerodha access tokens expire daily). After
`PRAMANA_UNPROTECTED_HALT_SECONDS` (120 by default) of an open position staying
unpriceable, the engine latches `protection_unreachable:<symbol>` and stops adding risk.
It does not liquidate: selling on a feed it cannot price is the fabricated-mark
behaviour the exit engine exists to refuse. Fix the feed, confirm ticks are arriving,
then `pramana resume`.

**Halts** — after a breach, BUYs show `max_drawdown_reached` /
`daily_loss_limit_reached`; SELLs show `approved_risk_reducing`. The drawdown tile
tracks the persisted peak. Kill and restart the daemon once mid-session: peak,
positions and cooldowns must survive; no duplicate fill on the next tick.

**Proofs** — every trade row shows EXACT PROOF; every proof carries
`founder_directives=…` when instructions are set.

## Daily routine and the decision record

Two operator actions per trading day, both on the host:

```bash
pramana zerodha-login                  # after 06:00 IST, before 09:15 IST; then restart the daemon
pramana post-mortem                    # after the close, writes the session review as "pending"
pramana post-mortem --approve YYYY-MM-DD   # after you have read it
```

Every cadence decision is journalled with its stance, confidence, regime and
governance outcome, and its forward returns are resolved afterwards, so the burn-in
produces measurable numbers rather than impressions. The dashboard shows them under
**Decision quality**, and `pramana decision-quality` prints the same report. Only
lessons from post-mortems you have approved ever reach the consensus prompt, and
they reach it as data inside the untrusted-evidence block.

Read `docs/DECISION_QUALITY.md` for what each number means and for the six criteria
this pilot must satisfy before a live adapter is worth discussing. Until at least 20
directional decisions have a resolved 60-minute outcome, the report says
`insufficient_sample` and none of its numbers should be read as edge.

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
