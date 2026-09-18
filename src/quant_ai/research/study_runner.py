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
from quant_ai.research.feature_study import run_feature_study
from quant_ai.validation.trial_register import record_trials, register_summary

SCHEMA = "pramana.feature_study_run.v1"
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
