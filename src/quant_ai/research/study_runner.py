"""Run the feature study over a real historical dataset, with the honest path the only path.

``backtesting/history.py`` already fetches years of daily bars and writes them in a shape
``backtesting/replay.py`` reads back. This joins that to the research loop, and it refuses to
let the result look better than the data it came from:

* The universe is audited before anything is scored. A dataset built from a watchlist is
  survivor-only by construction - it is a list of instruments that exist today, chosen by
  someone who knows they exist - and the report says so rather than leaving a reader to
  assume otherwise.
* Trials are registered BEFORE the verdict is computed, and the verdict is charged against
  the register's cumulative count. Running the same study again over the same bars is
  looking at them again, and the bar rises accordingly.

Example::

    python -m quant_ai.research.study_runner \\
        --dataset datasets/INFY.json --output reports/infy-study.json

Nothing here approves anything, and no arrangement of these numbers is permission to trade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from quant_ai.backtesting.replay import dataset_instrument, load_replay_dataset
from quant_ai.config import paths
from quant_ai.features.library import CORE_LIBRARY, FeatureLibrary
from quant_ai.marketdata.point_in_time import Listing, PointInTimeUniverse
from quant_ai.marketdata.universe_manifest import load_universe_manifest
from quant_ai.research.feature_study import run_feature_study
from quant_ai.validation.trial_register import record_trials, register_summary

SCHEMA = "pramana.feature_study_run.v1"
UNIVERSE_SCHEMA = "pramana.universe_study_run.v1"
STUDY = "core_feature_library"


def audit_dataset_universe(bars: Any, symbol: str, market: str) -> dict[str, Any]:
    """Grade the instrument set the study ran on.

    A single-instrument dataset is the strongest possible form of survivorship bias: one
    surviving name, chosen today. The audit is run anyway rather than skipped, because a
    report that simply omits the universe reads as a report whose universe was fine.
    """
    first, last = bars[0].timestamp.date(), bars[-1].timestamp.date()
    universe = PointInTimeUniverse(
        [Listing(symbol, market, first)],
        source="replay-dataset",
        coverage_from=first,
        coverage_to=last if last > first else date.fromordinal(first.toordinal() + 1),
    )
    return universe.audit().as_evidence()


def run(
    dataset: Path,
    *,
    register: Path,
    library: FeatureLibrary | None = None,
    horizon: int = 5,
    splits: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    catalogue = library or CORE_LIBRARY
    instrument = dataset_instrument(dataset)
    if instrument is None:
        raise ValueError(f"dataset does not name the instrument it holds: {dataset}")
    bars = load_replay_dataset(dataset, instrument).bars
    if len(bars) < 2:
        raise ValueError(f"dataset holds {len(bars)} bars; a study needs a history")

    digest = hashlib.sha256(Path(dataset).read_bytes()).hexdigest()

    # Registered first. A verdict computed before the run was recorded would be charged for
    # one fewer search than actually happened, and the difference is exactly the bias.
    record_trials(
        register,
        study=STUDY,
        candidate_trials=catalogue.hypothesis_count,
        configuration={
            "symbol": instrument.symbol,
            "market": instrument.market.value,
            "horizon": horizon,
            "splits": splits,
            "features": catalogue.hypothesis_count,
        },
        data_sha256=digest,
        now=now,
    )
    summary = register_summary(register, study=STUDY)
    cumulative = int(summary["candidate_trials"])

    study = run_feature_study(
        bars, catalogue, horizon=horizon, splits=splits, charged_trials=cumulative
    )
    universe = audit_dataset_universe(bars, instrument.symbol, instrument.market.value)

    blockers = list(study.reasons)
    if universe["verdict"] != "plausible":
        blockers.append(
            f"universe is not research-grade ({universe['verdict']}): a result measured on "
            "it cannot be trusted however the statistics read"
        )

    return {
        "schema": SCHEMA,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "symbol": instrument.symbol,
        "market": instrument.market.value,
        "bars": len(bars),
        "first_bar": bars[0].timestamp.isoformat(),
        "last_bar": bars[-1].timestamp.isoformat(),
        "data_sha256": digest,
        "library": catalogue.as_evidence(),
        "universe": universe,
        "trial_register": summary,
        "study": study.as_evidence(),
        "clears_every_gate": not blockers,
        "blockers": blockers,
        "promotion_approved": False,
        "limitation": (
            "A feature study over one instrument's daily bars. It is not a backtest, carries "
            "no cost or capacity model, and never approves promotion or live trading."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--splits", type=int, default=5)
    parser.add_argument("--trial-register", type=Path)
    args = parser.parse_args()

    report = run(
        args.dataset,
        register=args.trial_register or paths.trial_register(),
        horizon=args.horizon,
        splits=args.splits,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({
        "report": str(args.output),
        "symbol": report["symbol"],
        "bars": report["bars"],
        "cumulative_candidate_trials": report["trial_register"]["candidate_trials"],
        "deflated_sharpe": report["study"]["deflated_sharpe"],
        "universe_verdict": report["universe"]["verdict"],
        "clears_every_gate": report["clears_every_gate"],
        "promotion_approved": False,
    }))


if __name__ == "__main__":
    main()


def scope_to_tradeable(
    bars: Any, universe: PointInTimeUniverse, symbol: str
) -> tuple[list[Any], int, int]:
    """Keep only the bars for days the instrument could actually have been traded.

    Returns the kept bars, how many were dropped for falling on or after the delisting date,
    and how many fell outside the manifest's coverage entirely. Both counts are reported
    rather than swallowed: bars priced after a delisting are a data-integrity problem, and a
    study that silently trades them is exactly the survivorship the universe exists to stop.
    """
    kept, after_delisting, outside_coverage = [], 0, 0
    for bar in bars:
        day = bar.timestamp.date()
        try:
            tradeable = universe.was_tradeable(symbol, day)
        except ValueError:
            outside_coverage += 1
            continue
        if tradeable:
            kept.append(bar)
        else:
            after_delisting += 1
    return kept, after_delisting, outside_coverage


def run_universe_study(
    datasets: Path,
    manifest: Path,
    *,
    register: Path,
    library: FeatureLibrary | None = None,
    horizon: int = 5,
    splits: int = 5,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Study every instrument in a point-in-time universe, charged for the whole search.

    The single-dataset runner charges for the library. Studying fifty names charges for fifty
    searches, because reporting the best of fifty is fifty chances to find something and the
    bar has to know that. Without it, adding instruments would be a way to manufacture a
    result that adding features is already prevented from manufacturing.
    """
    catalogue = library or CORE_LIBRARY
    universe = load_universe_manifest(manifest)
    audit = universe.audit().as_evidence()

    files = sorted(Path(datasets).glob("*.json"))
    if not files:
        raise ValueError(f"no datasets found in {datasets}")

    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in files:
        instrument = dataset_instrument(path)
        if instrument is None:
            skipped.append({"dataset": path.name, "reason": "declares no instrument"})
            continue
        if instrument.symbol not in {item.symbol for item in universe.listings}:
            # Never silently studied. A dataset outside the universe is the survivorship
            # leak arriving by the back door.
            skipped.append({
                "dataset": path.name,
                "reason": f"{instrument.symbol} is not in the universe manifest",
            })
            continue
        bars = load_replay_dataset(path, instrument).bars
        kept, after_delisting, outside = scope_to_tradeable(bars, universe, instrument.symbol)
        prepared.append({
            "path": path,
            "instrument": instrument,
            "bars": kept,
            "bars_dropped_after_delisting": after_delisting,
            "bars_outside_coverage": outside,
            "data_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })

    if not prepared:
        raise ValueError("no dataset in this directory belongs to the universe manifest")

    record_trials(
        register,
        study=STUDY,
        candidate_trials=catalogue.hypothesis_count * len(prepared),
        configuration={
            "instruments": len(prepared),
            "features": catalogue.hypothesis_count,
            "horizon": horizon,
            "splits": splits,
            "universe_source": universe.source,
        },
        data_sha256=hashlib.sha256(
            "".join(sorted(item["data_sha256"] for item in prepared)).encode()
        ).hexdigest(),
        now=now,
    )
    summary = register_summary(register, study=STUDY)
    cumulative = int(summary["candidate_trials"])

    results = []
    for item in prepared:
        study = run_feature_study(
            item["bars"], catalogue, horizon=horizon, splits=splits, charged_trials=cumulative
        )
        results.append({
            "symbol": item["instrument"].symbol,
            "bars": len(item["bars"]),
            "bars_dropped_after_delisting": item["bars_dropped_after_delisting"],
            "bars_outside_coverage": item["bars_outside_coverage"],
            "data_sha256": item["data_sha256"],
            "study": study.as_evidence(),
        })

    graded = [r for r in results if r["study"]["deflated_sharpe"] is not None]
    best = max(graded, key=lambda r: r["study"]["deflated_sharpe"]) if graded else None

    blockers: list[str] = []
    if audit["verdict"] != "plausible":
        blockers.append(
            f"universe is not research-grade ({audit['verdict']}): a result measured on it "
            "cannot be trusted however the statistics read"
        )
    if best is None:
        blockers.append("no instrument produced a gradeable study")
    elif not best["study"]["clears_statistical_gate"]:
        blockers.extend(best["study"]["reasons"])

    return {
        "schema": UNIVERSE_SCHEMA,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "universe": audit,
        "universe_source": universe.source,
        "instruments_studied": len(prepared),
        "instruments_skipped": skipped,
        "library": catalogue.as_evidence(),
        "trial_register": summary,
        "best": None if best is None else best["symbol"],
        "results": results,
        "clears_every_gate": not blockers,
        "blockers": blockers,
        "promotion_approved": False,
        "limitation": (
            "Studies each instrument separately and reports the best, charged for every name "
            "and every feature the search covered. It is not a portfolio backtest, carries no "
            "cost or capacity model, and never approves promotion or live trading."
        ),
    }
