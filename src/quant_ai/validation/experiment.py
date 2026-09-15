"""Causal, deterministic baseline research; never a live-trading promotion approval.

CSV input: timestamp,close (timezone-aware, increasing, positive adjusted prices).
Example: python -m quant_ai.validation.experiment --data prices.csv --output report.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from random import Random

from quant_ai.config import paths
from quant_ai.validation.trial_register import record_trials, register_summary
from quant_ai.validation.walk_forward import walk_forward_splits


def path_metrics(returns: list[float]) -> dict:
    equity = peak = 1.0
    drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return {"net_return": equity - 1, "max_drawdown": drawdown, "observations": len(returns)}


def block_paths(returns: list[float], simulations: int = 1000, block: int = 5, seed: int = 7) -> dict:
    if not returns or not 1 <= block <= len(returns) or not 1 <= simulations <= 10000:
        raise ValueError("Invalid bootstrap sample or dimensions")
    rng = Random(seed)
    outcomes, drawdowns = [], []
    for _ in range(simulations):
        sample = []
        while len(sample) < len(returns):
            start = rng.randrange(len(returns) - block + 1)
            sample.extend(returns[start:start + block])
        result = path_metrics(sample[:len(returns)])
        outcomes.append(result["net_return"])
        drawdowns.append(result["max_drawdown"])
    outcomes.sort()
    drawdowns.sort()
    return {"method": "moving_block_bootstrap", "block_observations": block, "seed": seed,
            "simulations": simulations, "terminal_return_p05": outcomes[int(.05 * (simulations - 1))],
            "terminal_return_p50": outcomes[simulations // 2],
            "max_drawdown_p95": drawdowns[int(.95 * (simulations - 1))],
            "loss_frequency": sum(x < 0 for x in outcomes) / simulations,
            "limitation": "Resamples observed dependence within blocks; does not model unseen market regimes."}


def strategy_returns(prices: list[float], start: int, end: int, window: int, cost_bps: float) -> list[float]:
    """Signal at close t-1 earns t-1 to t return. Position starts and ends in cash."""
    held = 0
    returns = []
    for t in range(start, end):
        history = prices[max(0, t - window):t]
        desired = int(len(history) == window and history[-1] > sum(history) / window)
        turnover = abs(desired - held)
        market_return = prices[t] / prices[t - 1] - 1
        returns.append(desired * market_return - turnover * cost_bps / 10000)
        held = desired
    if returns:
        returns[-1] -= held * cost_bps / 10000
    return returns


def evaluate(prices: list[float], *, train: int = 60, test: int = 20,
             holdout: int = 40, windows: tuple[int, ...] = (5, 10, 20, 50), cost_bps: float = 10) -> dict:
    if (not windows or min(windows) < 2 or len(set(windows)) != len(windows)
            or train < max(windows) + 2 or min(test, holdout) < 5
            or not math.isfinite(cost_bps) or not 0 <= cost_bps < 1000
            or any(not math.isfinite(p) or p <= 0 for p in prices)
            or len(prices) < train + test + holdout):
        raise ValueError("Insufficient data or invalid experiment configuration")
    cutoff = len(prices) - holdout
    splits = walk_forward_splits(cutoff, train, test)
    folds, forward = [], []
    for split in splits:
        # Every candidate sees training data only. The test interval is disjoint.
        scores = {w: path_metrics(strategy_returns(prices, split.train_start + max(windows), split.train_end, w, cost_bps))["net_return"] for w in windows}
        selected = max(windows, key=lambda w: (scores[w], -w))
        values = strategy_returns(prices, split.test_start, split.test_end, selected, cost_bps)
        forward.extend(values)
        folds.append({"train": [split.train_start, split.train_end], "test": [split.test_start, split.test_end],
                      "selected_window": selected, "training_scores": scores, "test_metrics": path_metrics(values)})
    final_scores = {w: path_metrics(strategy_returns(prices, max(windows), cutoff, w, cost_bps))["net_return"] for w in windows}
    chosen = max(windows, key=lambda w: (final_scores[w], -w))
    held_out = strategy_returns(prices, cutoff, len(prices), chosen, cost_bps)
    baseline = [prices[t] / prices[t - 1] - 1 for t in range(cutoff, len(prices))]
    baseline[0] -= cost_bps / 10000
    baseline[-1] -= cost_bps / 10000
    return {"schema": "pramana.research.v1", "created_at": datetime.now(timezone.utc).isoformat(),
            "strategy": "long_cash_sma_baseline", "scope": "deterministic baseline, not the AI swarm",
            "configuration": {"train": train, "test": test, "holdout": holdout, "windows": windows, "cost_bps_per_turnover": cost_bps},
            "candidate_trials": len(windows) * (len(folds) + 1), "folds": folds,
            "walk_forward": path_metrics(forward), "holdout": {"range": [cutoff, len(prices)], "selected_window": chosen, **path_metrics(held_out)},
            "buy_and_hold": path_metrics(baseline), "cash_baseline": {"net_return": 0},
            "path_stress": block_paths(held_out), "promotion_approved": False,
            "limitations": ["Cost estimate is a configurable all-in turnover assumption, not a broker fill model.",
                            "Input must be licensed, point-in-time and corporate-action-consistent.",
                            "Trying another configuration reuses the holdout; every run is appended to the trial register and no reported statistic here is corrected for that multiplicity.",
                            "This baseline does not validate AI decisions, intrabar fills or future profits."]}


def load_prices(source: Path) -> list[float]:
    with source.open(newline="") as file:
        rows = list(csv.DictReader(file))
    times = [datetime.fromisoformat(row["timestamp"]) for row in rows]
    if any(t.tzinfo is None for t in times) or any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError("Timestamps must be timezone-aware and strictly increasing")
    return [float(row["close"]) for row in rows]


STUDY = "long_cash_sma_baseline"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=10)
    parser.add_argument(
        "--trial-register",
        type=Path,
        help="append-only trial register (default: beside the paper ledger)",
    )
    args = parser.parse_args()
    report = evaluate(load_prices(args.data), cost_bps=args.cost_bps)
    report["data_sha256"] = hashlib.sha256(args.data.read_bytes()).hexdigest()
    report["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # Register the run before the report is written. Re-running this study over another
    # window therefore leaves a trace, and the report carries the cumulative count of
    # candidates evaluated so far - the number every reported statistic has to answer for.
    register = args.trial_register or paths.trial_register()
    record_trials(
        register,
        study=STUDY,
        candidate_trials=int(report["candidate_trials"]),
        configuration=report["configuration"],
        data_sha256=report["data_sha256"],
    )
    report["trial_register"] = register_summary(register, study=STUDY)
    report["report_sha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as file:
        json.dump(report, file, indent=2)
    print(json.dumps({
        "report": str(args.output),
        "promotion_approved": False,
        "trial_register": str(register),
        "cumulative_candidate_trials": report["trial_register"]["candidate_trials"],
    }))


if __name__ == "__main__":
    main()
