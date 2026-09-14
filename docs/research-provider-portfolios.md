# Provider comparison, continuous portfolios and company events

This additive research package extends PR #51. It does not change PR #50's engine,
authenticated workspace, protective exits or deployment. None of these commands can
submit a broker order or promote a model. Integration into the hosted Research page
is a separate UI change; these modules are usable through the Python API and CLI.

## Provider comparison

`providers.evaluate_case` reads a frozen ResearchLab case and sends identical input,
instructions and output fields to Anthropic Messages and OpenAI Responses. The cash
baseline receives the same case. Valid BUY/HOLD advice, refusal, invalid output,
HTTP failure, usage, latency, requested and returned model, response hash and evidence
IDs persist with the research records. No search tools or execution tools are exposed.

Use an experiment created through the existing ResearchLab CLI. Add this configuration
before creation (the full original required configuration is still required):

~~~json
{
  "providers": {
    "claude": {"provider": "anthropic", "model": "claude-sonnet-5"},
    "astra": {"provider": "openai", "model": "gpt-6-astra"}
  }
}
~~~

Model names are explicit deployment choices; verify availability for your API account.
Optional `provider_rates` is keyed by candidate and contains `input_per_million`,
`output_per_million`, and, when applicable, `cached_input_per_million` and
`cache_creation_per_million`. Rates are frozen with the experiment. Missing rates or
usage produce a null cost and an incomplete total, never a claimed zero-cost call.
USD model costs are reported separately from INR portfolio P&L.

~~~sh
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py compare research.sqlite \
  --experiment my-experiment --case-id case-001 --receipts /private/path/research-receipts
~~~

Credentials come from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, falling back to the
private `~/.config/pramana/anthropic.key` / `openai.key` files. Never put them in
experiment JSON, a web client, git or command arguments.

One bounded request per candidate/case, no automatic retries. The receipt directory
must be reused. Exclusive durable receipt creation prevents duplicate workers from
issuing the same paid request. A crash can leave a pending or response-recorded
receipt before the database write; an operator must reconcile that receipt rather
than delete it and retry. Raw successful responses are private receipt evidence.
Provider errors retain exception type, not potentially sensitive HTTP error text.

Historical tests cannot exclude knowledge already present in model training. For
prospective comparison, freeze cases as evidence arrives, before future prices exist.
Model aliases may change at the provider; inspect returned-model records and start
a new experiment when versions change. An unavailable provider is a recorded failure,
not an invented prediction or a successful cash decision.

## Continuous portfolios

`PortfolioJournal` uses its own SQLite file and refuses a paper/research ledger.
Each candidate has independent initial cash, positions, fees, realized P&L, peak
equity and drawdown on the same quote timeline. Configuration is immutable.
Reopening deterministically reconstructs the state. Event IDs are idempotent; an
ID with different content is rejected. Every event is validated before commit.

Orders fill only on a later quote, respecting side liquidity, cash, position and
gross exposure caps, costs, expiry and session status. Each order is immediate-or-cancel
at that quote: partial remainders are cancelled. SELL cannot exceed holdings. A
drawdown breach permanently blocks further buys for that simulation; exits remain
possible. Market gaps can still cause losses larger than the threshold.

Stale held-symbol marks make current equity and drawdown unavailable. The curve
preserves previous observations; it does not relabel them as current. Quotes use bid
marks and ask entry prices, with configurable adverse slippage and fees.

Input JSON: `{"config": {...}, "events": [...]}`. Required config fields are
`candidates`, `symbols` (NSE:TICKER), `starting_cash_inr`, `fee_bps`, `slippage_bps`,
`max_position_fraction`, `max_gross_fraction`, `max_drawdown_fraction`,
`max_quote_age_seconds`, `order_ttl_seconds`, `max_order_quantity`.

Events have unique `id`, timezone-aware `at`, and `kind`:

- `quote`: `symbol`, positive `bid`, `ask`, integer `bid_quantity`, `ask_quantity`,
  boolean `session_open`, and `provenance`.
- `order`: `candidate`, `symbol`, `side` BUY/SELL, integer `quantity`, `decision_ref`.
- `clock`: advances time to reveal stale marks and expire pending orders.

~~~sh
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py simulate simulation.sqlite \
  --file timeline.json
~~~

Optional `--decisions-from research.sqlite --experiment my-experiment` imports
successful BUY decisions with their actual completion timestamps and evidence hashes.
The simulator does not invent exits. Supply a preregistered SELL policy's events;
unclosed holdings remain visible and are marked. A different exit policy requires
a separate experiment. This is long-only NSE equity/ETF research, without derivatives,
corporate-action processing, or a live-feed scheduler. Session state, adjustments and
liquidity must come from qualified input. Full replay per append favors auditability;
benchmark and checkpoint before scaling to high-frequency workloads.

## Company-event evidence

The connector targets NSE's official announcement RSS endpoint. It validates HTTPS
source links, blocks DTD/entity XML, caps responses at 5 MB, does not follow redirects,
and does not download or execute attachments. Timeout/403/invalid XML fails visibly;
no fallback synthesizes company events. Imported XML is labelled `imported`.

Each capture preserves raw bytes, hash, observation time and rejected-item reasons.
Each event revision preserves publication time and first-seen time. Repeated retrieval
cannot backdate availability. Corrections sharing a GUID supersede only after first
observation. Future-dated and non-official-link items are quarantined.

~~~sh
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py event-fetch events.sqlite
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py event-status events.sqlite
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py event-map events.sqlite --file mapping.json
PYTHONPATH=src .venv/bin/python scripts/research_extensions.py event-sources events.sqlite \
  --symbol NSE:INFY --at 2026-09-14T15:30:00+05:30
~~~

Mapping JSON: `exact_title`, `symbol`, `verified_at`, `provenance`. Match the feed's
actual company title to a reviewed instrument-master record; do not guess tickers
from headlines. Mapping availability is also respected at decision time. Unmapped
records remain stored but cannot become symbol-specific model evidence.

`sources_as_of` returns ResearchLab-compatible source items. Add them to a new case's
`sources` before freezing that case. Official publication provenance does not verify
the company's assertions. RSS is not comprehensive fundamentals, financial statements,
all BSE disclosures, a licensed point-in-time archive, or automatic price adjustments.

## Evidence required before integration closure

- Both live provider requests recorded successfully, with model/version and usage.
- Identical prospective inputs and future outcomes collected; no winner from a smoke test.
- Continuous simulations reconcile to independent cash/position/fee calculations.
- Official feed capture succeeds on the target host; actual title/date formats are checked.
- Instrument mappings and source rights are reviewed for the intended universe.
- PR #50 integration tests pass; hosted UI release references the exact reviewed commit.

Sources: [OpenAI structured output documentation](https://developers.openai.com/api/docs/guides/structured-outputs),
[Anthropic Messages API](https://platform.claude.com/docs/en/api/messages),
[NSE RSS documentation](https://www.nseindia.com/static/rss-feed),
[NSE corporate data](https://www.nseindia.com/static/market-data/corporate-data-subscription).


## Validation on 2026-09-14

- Full Python suite: 350 passed, including 37 new extension tests; focused Ruff passes.
- Simulator CLI replay/reopen is idempotent and reconciles a synthetic round trip.
- Real Claude Messages call succeeded: claude-sonnet-5, 1,028 input / 227 output tokens.
  The input was explicitly synthetic connection-test evidence, not a trading-performance test.
- Astra was recorded as credential_missing. OPENAI_API_KEY / openai.key is absent.
  Its request/response contract, refusal and error handling pass mocked transport tests;
  live Astra connectivity and a two-provider performance comparison remain unverified.
- NSE RSS HTTPS retrieval timed out on the Mac. Parser, revision, mapping and failure
  audit tests pass; live feed payload/coverage verification remains open.
- Private smoke evidence SHA-256:
  98246f4433144950c56a70c65ed7bef127284777e11ba4f725be3e548bf47c30.
- No hosted UI or broker/runtime configuration changed. No real order submitted.

- Temporary merge with PR #50 at 3900aa9ff70d0706ae913a6b41b1183ae53a7f5b
  was conflict-free; the combined Python suite passed all 448 tests. The temporary
  merge was discarded after verification; PR #50's branch was not modified.


## Private workspace integration in PR #50

The continuous journal now has a [read-only publication and dashboard workflow](PORTFOLIO_RESEARCH_WORKSPACE.md), with candidate equity/drawdown curves, preserved gaps, holdings, fills/orders, authenticated export and bounded Atlas context. Synthetic arithmetic and desktop/mobile/API verification pass. This updates the earlier CLI-only status; company-event mapping UI and real two-provider/live-source qualification remain unfinished. [Recovery schema 2](RECOVERY_BUNDLE.md) now supports the explicitly selected research journals, published files and provider receipts; complete deployment inventory and off-host/target-host restoration remain unqualified.
