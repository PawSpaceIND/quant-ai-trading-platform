# Item 2: remaining history integration and acceptance contract

## Status: design and handoff, not an implemented data source

Current Item 2 parent: `85369172a1b03ed8dbd936652350118a1d4a879a`.
Owner-approved main now includes #134 at `58a3d72a6fafbd17df97bccb78943a30be06df48`.
The cadence-cutoff fix changes tick selection, not daily-history availability.
The proposed Kite history adapter remains absent. No adapter implementation,
blocked risk-mutation runner, production snapshot read or provider request is
performed by this document. It does not change a permission or loosen a gate.

## Current source facts

- `daemon._env_daily_history_provider()` accepts Yahoo or none. The existing
  preflight constructs Yahoo directly, so it cannot exercise a nonexistent
  alternative merely because an environment variable names that alternative.
- `scripts/market_monitor.py` fetches Kite historical candles but retains only
  date/close observations for exploratory riskHistory. Do not manufacture
  open/high/low/volume from that output or treat it as the daemon's OHLCV feed.
- Reuse the existing instrument-master parser, Candle model, daily cache and
  DailyCloseHistory checks, but do not assume their use alone qualifies a feed.
- The last observed public-history run was 2026-09-17 09:22:53 UTC. It was rate
  limited and returned zero usable rows; it is historical evidence, not a new
  observation of the intended host. Do not retry just to obtain a green label.

## Documented API facts, checked 17 September 2026

Kite documents GET historical data with the `day` interval. Each ordinary candle
contains timestamp, open, high, low, close and volume. Instrument tokens come from
its instrument list; the master exposes exchange and tradingsymbol and is generated
once daily. The documentation recommends exchange plus tradingsymbol as the stored
identity, not a permanent assumption about a numeric token.

Primary documentation:
- https://kite.trade/docs/connect/v3/historical/
- https://kite.trade/docs/connect/v3/market-quotes/

These facts describe API shape. They do not prove the owner's current authentication,
account entitlement, data licensing, adjustment policy, available date range or actual
response quality. Do not copy the documentation's example instrument tokens into config.

## Proposed integration requirements (not built here)

1. Source selection is explicit and opt-in. Preserve the existing Yahoo default;
   selecting Kite must neither silently fall back nor change a live-money setting.
   Preflight and daemon must select the same adapter under the reviewed configuration.
2. The adapter performs bounded read-only requests only. Never add order methods,
   persist auth headers, echo credentials, or bypass rejected/expired authentication.
   Keep ordinary TLS and provider throttling. Unauthorized or rate-limited responses
   leave history unavailable; never rotate identities or switch sources to hide it.
3. Match the five scoped instruments (INFY, TCS, RELIANCE, GOLDBEES, SILVERBEES)
   against current exact NSE instrument-master rows. Reject missing, duplicate or
   conflicting identities; derive numeric tokens rather than guessing them.
4. Decode monetary values as Decimal, retain aware timestamps, and judge sessions in
   venue-local time. Preserve full OHLCV rows and their source identity. Reject
   contradictory, nonfinite, duplicate or invalid data. Do not fabricate sessions,
   prices, volume, corporate adjustments or absent fields.
5. Reuse the existing daily cache without extra requests under the protection lock.
   Empty/expired/failed observations must remain unavailable. A provider object or
   configured arming is not proof of sufficient aligned closed-session records.
6. Retain the current book-risk sample, mapping and freshness requirements. Compare
   the actual count with the constants in the reviewed source; do not lower those
   thresholds to fit a small or incomplete response. Record missing coverage openly.

## Evidence required before claiming this part complete

| Gate | Required evidence | Current status |
| --- | --- | --- |
| Source implementation | Reviewed explicit selection shared by daemon and preflight | Missing |
| Offline correctness | Executed identity, precision, timestamp, invalid-data, cache and no-order tests | Not executed for new adapter |
| Guard verification | Each added guard deliberately broken in an approved isolated test context; failing named test and restored source | Not executed for new adapter |
| Real source | Actual permitted response for all five instruments; acquisition time, source, retained hash, last closed sessions and coverage | Missing |
| Price basis | Provider adjustments, missing-data and corporate-action limitations retained; no unsourced claims | Unqualified |
| Preflight | Exact source/config digests and usable aligned records; dataReady true without a fabricated override | Missing |
| Running host | Owner-controlled deployed revision, loaded configuration, actual cached records and three operational risk gates | Not observed |
| Covered exits | Recorded protective/reducing-exit behavior under missing-history conditions on the approved host/test scope | Not accepted |

The existing 51-case production risk-guard inventory remains 0 executed / 0 certified.
Earlier deployment, preflight and cadence test campaigns do not substitute for it.
The execution tool previously blocked the adapter write and the risk runner; this
handoff does not reissue, split, disguise or route around those blocked operations.
A safe, explicitly permitted execution context is still needed for those steps.

Owner review, merging and deployment remain separate. Do not start Item 3 before
owner merge of Item 2, do not advance MCX admission, and do not raise any risk cap.
