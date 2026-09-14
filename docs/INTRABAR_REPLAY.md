# Lower-timeframe protective-exit replay

The historical JSON replay accepts optional `intrabar_windows`. When supplied, every execution parent after the first decision bar must have exactly one complete window. CSV and JSON without windows retain the existing behavior and explicitly report `protection_model: "not_simulated"` in the tearsheet.

Example window (alongside matching parent `bars` in the JSON dataset):

```json
{
  "intrabar_windows": [{
    "parent_timestamp": "2026-09-15T04:17:00+00:00",
    "start": "2026-09-15T04:15:00+00:00",
    "interval_seconds": 60,
    "bars": [
      {"timestamp": "2026-09-15T04:16:00+00:00", "open": 100, "high": 104, "low": 96, "close": 101, "volume": 1000},
      {"timestamp": "2026-09-15T04:17:00+00:00", "open": 101, "high": 111, "low": 99, "close": 105, "volume": 1000}
    ]
  }]
}
```

Timestamps are interval **closes**, not opens. Convert vendor timestamps explicitly before import. Window starts may skip closed-market periods but cannot precede the prior decision close. Lower bars must be chronological, continuous at the declared positive integer interval, instrument-matched, end at the parent close and reconcile parent open/high/low/close. Missing, duplicate, overlapping or inconsistent windows reject the dataset before replay mutations. There is no invented parent-bar path fallback.

## Execution rules

- Long-position protection uses chronological lower OHLC bars. An opening gap through a stop uses the observed open plus the existing broker friction model, so the simulated fill can be worse than the stop.
- Existing protection at a parent opening gap executes before discretionary analysis; that parent suppresses a new discretionary action. New entries are then eligible for protection within the same parent.
- If stop and target are touched in the same lower bar with no resolving opening gap, the stop wins and the event is marked ambiguous. An earlier target in a preceding lower bar wins over a later stop.
- Targets are market-style triggers, not guaranteed limit fills. Protective exits remain paper-only, persist in the ledger, carry a cooldown, and are linked to explicit events in the JSON tearsheet. They are not full AI XAI proofs.
- Tearsheets expose `protection_model` and `intrabar_exits`, including order ID, reference price, interval bounds, gap and ambiguity flags. Interior events use interval-end bookkeeping; that is not a measured tick execution time.

## Limits and remaining qualification

This improves one execution-model gap against the TradingView Bar Magnifier reference; it does not establish full product parity. Queue position, partial fills, exchange limits, latency paths, tick ordering, short positions and broker-native order lifecycles are not simulated here. Liquidity friction uses preceding parent bars, not a measured execution-time order book. Child volumes are not used to assert market-depth capacity.

The existing replay pipeline uses prior-close features with next-open pricing for sizing and proposal/risk evaluation. It is not a frozen prior-close order submitted unchanged at the next open. Keep that distinction when comparing strategies; next-open prices must never be described as known at the prior close. The deterministic SMA research experiment is a separate path and does not automatically gain this execution model.

Synthetic regression tests verify rules and ledger effects. Real lower-timeframe data, strategy-specific holdout evaluation and forward-feed fill comparisons remain required before effectiveness or launch-readiness claims.
