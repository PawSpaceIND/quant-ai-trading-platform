"""Regenerate tests/fixtures/decision-quality-engine.json from the engine's own builder.

Run from the repository root:

    python3 apps/pramana-ui/scripts/generate-decision-quality-fixture.py \
      > apps/pramana-ui/tests/fixtures/decision-quality-engine.json

Hand-written fixtures are how the parser drifted from the report in the first place:
sections the engine had started writing were never added to the fixture, so no test
noticed the reader dropping them. This one is produced by the engine's own builder.
"""
import json
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "src")
from quant_ai.analytics.decision_quality import summarize

IST = timezone(timedelta(hours=5, minutes=30))
BASE = datetime(2026, 9, 14, 9, 20, tzinfo=IST)

PLAYBOOKS = ["trend_following", "ranging", "high_volatility"]
REGIMES = ["TRENDING_UP", "RANGE_BOUND", "HIGH_VOLATILITY"]
STANCES = ["BUY", "SELL", "STRONG_BUY", "NEUTRAL"]
SYMBOLS = ["TCS", "INFY", "HDFCBANK", "RELIANCE"]

rows = []
for i in range(48):
    stance = STANCES[i % 4]
    directional = stance != "NEUTRAL"
    # Deterministic but dispersed, so both t-statistics are defined.
    forward = round(((i % 11) - 5) * 0.0013 + 0.0004, 6) if directional else None
    filled = directional and i % 12 != 11
    closed = filled and i % 9 != 8
    session_day, slot = divmod(i, 8)
    rows.append({
        "decision_id": f"d-{i:04d}",
        "decided_at": (BASE + timedelta(days=session_day, minutes=47 * slot)).isoformat(),
        "symbol": SYMBOLS[i % 4],
        "stance": stance,
        "confidence": str(round(0.45 + (i % 10) * 0.05, 4)),
        "regime": REGIMES[i % 3],
        "playbook": PLAYBOOKS[i % 3],
        "mode": "llm" if i % 2 else "deterministic",
        "governance": "filled" if filled else ("rejected" if i % 3 == 1 else "abstained"),
        "reason": None if filled else ("conviction_below_floor" if i % 3 == 1 else "no_actionable_signal"),
        "order_id": f"ord-{i:04d}" if filled else None,
        "forward_return_60m": None if forward is None else str(forward),
        "realized_net_pnl": str(round(((i % 9) - 4) * 137.5 + 22.5, 2)) if closed else None,
        "exit_trigger": ("take_profit" if i % 4 == 0 else "stop_loss") if closed else None,
        # Exploration probes: below the conviction floor, allowed by the daily budget.
        "probe": 1 if directional and i % 7 == 0 else 0,
        "agents": json.dumps({"macro": {"stance": stance}, "technical": {"stance": "NEUTRAL"}}),
        "inference_status": "completed" if i % 2 else "invalid_schema",
        "inference_failure_code": None if i % 2 else "missing_consensus_fields",
    })

# The directional forecast the engine records on every decision: the probability that the
# forward return clears a stated cost over a stated horizon, and the named mapping that
# produced it. Bases are never pooled, so the fixture carries a retired mapping and its
# refit, and the reader has to keep them apart.
for i, row in enumerate(rows):
    # One decision states no forecast at all and one names a horizon with no resolver
    # column, so the unscoreable counters are exercised rather than assumed to be zero.
    if i == 3:
        continue
    # Deterministic and dispersed across every reliability bin, with two values inside one
    # bin so the within-bin term of the decomposition is non-zero and gets checked.
    probability = round(0.05 + (i % 11) * 0.09, 4)
    decided_at = datetime.fromisoformat(row["decided_at"])
    horizon = 7200 if i == 5 else 3600
    row.update({
        "forecast_probability_up": str(probability),
        "forecast_horizon_seconds": horizon,
        "forecast_cost_bps": "5",
        "forecast_resolves_at": (decided_at + timedelta(seconds=horizon)).isoformat(),
        "forecast_basis": "consensus_lean_v1" if i < 44 else "consensus_lean_v2",
    })

now = BASE + timedelta(days=7)
report = summarize(rows, tenant_id="ghost", now=now, since=now - timedelta(days=30), minimum_sample=20)
print(json.dumps(report, indent=2))
