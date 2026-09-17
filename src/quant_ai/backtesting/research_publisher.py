"""Publish a fixed train/holdout price-rule study, never a claim about the traded AI.

Money and compounded bootstrap paths use Decimal. Only final dimensionless UI metrics
become finite JSON numbers. No broker state, credentials, live orders or promotion.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from quant_ai.analytics.metrics import maximum_drawdown
from quant_ai.backtesting.baselines import (
    MINIMUM_RATIO_OBSERVATIONS,
    MINIMUM_ROUND_TRIPS,
    BaselineEvaluator,
    BaselineRun,
    BuyAndHoldBaseline,
    TimeSeriesMomentumBaseline,
    _assert_daily_bars,
)
from quant_ai.backtesting.contest import NOT_THE_AI
from quant_ai.backtesting.replay import (
    TRADED_CONFIGURATION_DIFFERENCES,
    dataset_instrument,
    load_replay_dataset,
)
from quant_ai.config import paths
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import session_date, venue_for
from quant_ai.operations.evidence_log import read_records
from quant_ai.validation.trial_register import record_trials, register_summary

SCHEMA = "pramana.research.v1"
PROTOCOL = "chronological_70_30_cold_start_momentum_v1"
LOOKBACKS = (5, 10, 20)
TRAIN_FRACTION = Decimal("0.70")
STARTING_CAPITAL = Decimal(100000)
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_BLOCK_LENGTH = 5
BOOTSTRAP_SEED = 1729
MAX_REPORT_BYTES = 2_000_000
SIBLING_PANELS = {
    "PRAMANA_RESEARCH_LAB_REPORT": (
        "Not emitted: needs registered matched cases, candidate decisions, resolved "
        "outcomes and measured API costs, not price-rule baselines."
    ),
    "PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT": (
        "Not emitted: needs sanitized comparison, simulation and company-event exports; "
        "this single-instrument study supplies none."
    ),
    "PRAMANA_PORTFOLIO_RESEARCH_REPORT": (
        "Not emitted: needs a continuous multi-asset quote/order journal and reconciled "
        "books; independent daily baselines are not that journal."
    ),
}


class ResearchPublicationRefused(ValueError):
    """Evidence cannot support publication. No new report is installed."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ResearchPublicationRefused(reason)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def bar_digest(bars: tuple[Candle, ...]) -> str:
    """Same ordered OHLCV digest convention as cli._bar_digest/contest."""
    return _sha("|".join(
        f"{b.timestamp.isoformat()}:{b.open}:{b.high}:{b.low}:{b.close}:{b.volume}"
        for b in bars
    ).encode())


def _number(value: Decimal) -> float:
    _require(value.is_finite(), "research_metric_not_finite")
    result = float(value)
    _require(math.isfinite(result), "research_metric_not_finite")
    return result


def _period(bars: tuple[Candle, ...]) -> dict[str, Any]:
    venue = venue_for(bars[0].instrument.market)
    return {
        "first_timestamp": bars[0].timestamp.isoformat(),
        "last_timestamp": bars[-1].timestamp.isoformat(),
        "first_local_date": session_date(bars[0].timestamp, venue).isoformat(),
        "last_local_date": session_date(bars[-1].timestamp, venue).isoformat(),
        "bars": len(bars), "data_sha256": bar_digest(bars),
    }


def _assert_split(
    bars: tuple[Candle, ...], train: tuple[Candle, ...], holdout: tuple[Candle, ...]
) -> None:
    _require(bool(train) and bool(holdout), "research_empty_split")
    _require(train + holdout == bars, "research_split_not_partition")
    venue = venue_for(bars[0].instrument.market)
    train_dates = {session_date(b.timestamp, venue) for b in train}
    holdout_dates = {session_date(b.timestamp, venue) for b in holdout}
    _require(
        not train_dates.intersection(holdout_dates) and max(train_dates) < min(holdout_dates),
        "research_holdout_overlaps_training",
    )


def _assert_evidence_floor(run: BaselineRun) -> None:
    _require(
        len(run.returns) >= MINIMUM_RATIO_OBSERVATIONS,
        f"research_observations_below_minimum:{MINIMUM_RATIO_OBSERVATIONS}",
    )
    _require(
        len(run.round_trips) >= MINIMUM_ROUND_TRIPS,
        f"research_round_trips_below_minimum:{MINIMUM_ROUND_TRIPS}",
    )


def _assert_run(run: BaselineRun, bars: tuple[Candle, ...]) -> None:
    _require(
        len(run.equity_curve) == len(bars) and len(run.returns) == len(bars) - 1,
        "research_run_does_not_match_scored_bars",
    )
    _require(
        run.equity_curve[0] == STARTING_CAPITAL
        and all(v.is_finite() and v > 0 for v in run.equity_curve),
        "research_invalid_equity_path",
    )
    expected = tuple(
        (after - before) / before
        for before, after in zip(run.equity_curve, run.equity_curve[1:])
    )
    _require(run.returns == expected, "research_return_path_mismatch")


def path_stress(returns: tuple[Decimal, ...]) -> dict[str, Any]:
    """Circular five-bar bootstrap; nearest-rank p95 of 1000 compounded drawdowns.

    Resamples net account returns, not fills. Equality with observed drawdown can be real;
    the independent bootstrap test detects an observed-drawdown shortcut, not equality.
    """
    _require(len(returns) >= MINIMUM_RATIO_OBSERVATIONS, "research_stress_too_short")
    _require(
        all(isinstance(r, Decimal) and r.is_finite() and r > -1 for r in returns),
        "research_stress_invalid_returns",
    )
    rng = random.Random(BOOTSTRAP_SEED)
    drawdowns: list[Decimal] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sampled: list[Decimal] = []
        while len(sampled) < len(returns):
            start = rng.randrange(len(returns))
            sampled.extend(
                returns[(start + offset) % len(returns)]
                for offset in range(BOOTSTRAP_BLOCK_LENGTH)
            )
        curve = [Decimal(1)]
        for r in sampled[:len(returns)]:
            curve.append(curve[-1] * (1 + r))
        drawdowns.append(maximum_drawdown(tuple(curve)))
    rank = (95 * len(drawdowns) + 99) // 100
    p95 = sorted(drawdowns)[rank - 1]
    return {
        "max_drawdown_p95": _number(p95),
        "method": "circular_moving_block_bootstrap_of_net_holdout_returns",
        "samples": BOOTSTRAP_SAMPLES, "block_length": BOOTSTRAP_BLOCK_LENGTH,
        "seed": BOOTSTRAP_SEED, "observations": len(returns),
        "quantile": "nearest_rank_ceiling_0.95_times_samples",
        "return_path_sha256": _sha(_canonical([str(r) for r in returns])),
        "drawdown_distribution_sha256": _sha(_canonical([str(d) for d in drawdowns])),
    }


def _limitations(train, holdout, trials, run, floor) -> list[str]:
    return [
        (
            f"Sample: training {train[0].timestamp.isoformat()} to {train[-1].timestamp.isoformat()}; "
            f"holdout {holdout[0].timestamp.isoformat()} to {holdout[-1].timestamp.isoformat()}. "
            "One chronological split, not independent forward validation."
        ),
        (
            "Single currently selected security: survivorship/selection bias is not corrected; "
            "delisted names and a point-in-time investable universe are absent."
        ),
        NOT_THE_AI + (
            " This report is a price-only momentum baseline, not even the deterministic "
            "swarm consensus. It does not measure the live AI strategy."
        ),
        (
            "Both holdout accounts restart flat with equal capital, no training P&L or bars "
            "carried over. Rule warm-up consumes holdout bars; fills use next-bar opens."
        ),
        (
            "Costs use the existing MarketFrictionModel and built-in fee/brokerage schedules "
            "across all dates, not independently verified historical contract-note rates. "
            "Spread and impact are modelled from closed bars, not observed historical bid/ask."
        ),
        (
            "Raw quote prices only; dividend-adjusted close is not consumed. Cash dividends, "
            "corporate-action cashflows and point-in-time split verification are not supplied; "
            "this is not a total-shareholder-return comparison."
        ),
        (
            f"The shared replay register records {trials} candidate trials as of the pinned "
            "snapshot. Prior backtest/contest looks may have exposed these dates; untouched "
            "holdout status and unregistered/deleted historical trials cannot be established. "
            "No multiple-testing correction or statistical significance claim is made."
        ),
        (
            "Path stress resamples net holdout returns in five-bar blocks. It is conditional "
            "on this path, not a forecast or confidence bound; it cannot invent unseen crashes "
            "or reproduce execution under a different price path."
        ),
        (
            f"End positions remain marked, not liquidated: rule open={run.open_position_at_end}, "
            f"buy-and-hold open={floor.open_position_at_end}. Any final exit friction is unpaid."
        ),
        (
            "Bars and metadata are operator-supplied; hashes establish identity, not independent "
            "market-data authenticity."
        ),
    ]


def _cost_assumptions(evaluator: BaselineEvaluator) -> dict[str, Any]:
    model = evaluator.friction_model
    return {
        "starting_capital": str(evaluator.starting_capital),
        "delivery": evaluator.delivery, "liquidity_score": str(evaluator.liquidity_score),
        "fee_schedule": {k: str(v) for k, v in asdict(model.fee_schedule).items()},
        "brokerage_schedule": {k: str(v) for k, v in asdict(model.brokerage_schedule).items()},
        "friction_parameters": {k: str(getattr(model, k)) for k in (
            "gamma", "spread_atr_multiplier", "max_slippage_fraction",
            "max_half_spread_fraction", "fixed_slippage_bps",
        )},
    }


def validate_report(report: dict[str, Any]) -> None:
    """research.ts's exact checks, plus required fields its cards consume."""
    _require(report.get("schema") == SCHEMA, "research_ui_schema_invalid")
    for section, keys in {
        "holdout": ("net_return", "max_drawdown", "observations"),
        "buy_and_hold": ("net_return",), "path_stress": ("max_drawdown_p95",),
    }.items():
        values = report.get(section)
        _require(isinstance(values, dict), "research_ui_section_missing")
        for key in keys:
            value = values.get(key)
            _require(
                type(value) in (int, float) and math.isfinite(value),
                "research_ui_metric_invalid",
            )
    for key in ("strategy", "scope", "created_at", "data_sha256"):
        _require(isinstance(report.get(key), str) and bool(report[key].strip()),
                 "research_ui_label_missing")
    stamp = datetime.fromisoformat(report["created_at"])
    _require(stamp.tzinfo is not None and stamp.utcoffset() is not None,
             "research_timestamp_not_aware")
    limitations = report.get("limitations")
    _require(
        isinstance(limitations, list) and len(limitations) > 0
        and all(isinstance(v, str) and bool(v.strip()) for v in limitations),
        "research_limitations_empty",
    )
    _require(type(report.get("candidate_trials")) is int and report["candidate_trials"] > 0,
             "research_trial_count_invalid")
    _require(len(_canonical(report)) <= MAX_REPORT_BYTES, "research_report_too_large")


def _atomic_write(path: Path, document: bytes) -> None:
    """Same-directory fsynced replace; failures preserve an earlier valid report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
            # Public research, no secrets; the dashboard's uid 10001 must be able to read.
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _register_snapshot(register: Path, study: str) -> tuple[dict, list[dict], str]:
    """Pin counts and the last chain record to the same unchanging file bytes."""
    original = register.read_bytes()
    summary = register_summary(register, study=study)
    records = read_records(register)
    _require(register.read_bytes() == original, "research_trial_register_changed_during_read")
    return summary, records, _sha(original)


def publish_research(
    bars: tuple[Candle, ...], *, instrument: Instrument, register: Path,
    output: Path, now: datetime | None = None, source_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Register every attempted candidate before selection; publish only on success."""
    moment = now or datetime.now(timezone.utc)
    _require(os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "false",
             "research_requires_paper_only")
    _require(moment.tzinfo is not None and moment.utcoffset() is not None,
             "research_timestamp_not_aware")
    _require(
        instrument.asset_class in (AssetClass.EQUITY, AssetClass.ETF)
        and ((instrument.market == Market.INDIA and instrument.exchange == "NSE"
              and instrument.currency == "INR")
             or (instrument.market == Market.USA and instrument.exchange in ("NASDAQ", "NYSE")
                 and instrument.currency == "USD")),
        "research_instrument_not_supported",
    )
    _assert_daily_bars(bars)
    _require(all(b.instrument == instrument for b in bars), "research_instrument_mismatch")
    _require(all(v.is_finite() for b in bars for v in (b.open, b.high, b.low, b.close, b.volume)),
             "research_nonfinite_bar")
    venue = venue_for(instrument.market)
    _require(session_date(bars[-1].timestamp, venue) < session_date(moment, venue),
             "research_unclosed_daily_bar")
    split = int(Decimal(len(bars)) * TRAIN_FRACTION)
    train, holdout = bars[:split], bars[split:]
    _assert_split(bars, train, holdout)
    _require(len(train) - 1 >= MINIMUM_RATIO_OBSERVATIONS, "research_training_too_short")
    _require(len(holdout) - 1 >= MINIMUM_RATIO_OBSERVATIONS, "research_holdout_too_short")
    _require(register.resolve() != output.resolve(), "research_output_overwrites_register")
    _require(register.is_file() and register.stat().st_size > 0,
             "research_existing_trial_register_required")
    study = f"replay:{instrument.symbol}:{instrument.market.value}"
    prior = register_summary(register, study=study)
    _require(prior["candidate_trials"] > 0, "research_existing_study_required")
    record_trials(
        register, study=study, candidate_trials=len(LOOKBACKS),
        configuration={"command": "publish-research", "protocol": PROTOCOL,
                       "lookbacks": list(LOOKBACKS), "train": _period(train),
                       "holdout": _period(holdout), "selection": "highest_training_net_return"},
        data_sha256=bar_digest(bars), now=moment,
    )
    trials, records, register_sha256 = _register_snapshot(register, study)
    evaluator = BaselineEvaluator(instrument=instrument, starting_capital=STARTING_CAPITAL)
    scored: list[tuple[Decimal, int]] = []
    training_results = []
    for lookback in LOOKBACKS:
        candidate = TimeSeriesMomentumBaseline(lookback=lookback)
        training_run = evaluator.run(candidate, train)
        _assert_run(training_run, train)
        score = (training_run.equity_curve[-1] - STARTING_CAPITAL) / STARTING_CAPITAL
        training_results.append({"lookback": lookback, "net_return": str(score),
                                 "observations": len(training_run.returns),
                                 "round_trips": len(training_run.round_trips)})
        if len(training_run.round_trips) >= MINIMUM_ROUND_TRIPS:
            scored.append((score, lookback))
    _require(bool(scored), "research_no_training_candidate_meets_floor")
    # Fixed stable tie-break; never inspect alternate candidates on the holdout.
    selected = max(scored, key=lambda pair: (pair[0], -pair[1]))[1]
    run = evaluator.run(TimeSeriesMomentumBaseline(lookback=selected), holdout)
    floor = evaluator.run(BuyAndHoldBaseline(), holdout)
    _assert_run(run, holdout)
    _assert_run(floor, holdout)
    _assert_evidence_floor(run)
    report = {
        "schema": SCHEMA,
        "strategy": f"baseline.momentum.v1 lookback={selected} (price-only rule, not the traded AI)",
        "scope": f"{instrument.exchange}:{instrument.symbol} {instrument.market.value}; paper research",
        "created_at": moment.astimezone(timezone.utc).isoformat(),
        "data_sha256": bar_digest(bars), "source_file_sha256": source_file_sha256,
        "candidate_trials": trials["candidate_trials"],
        "trial_register": {"study": study, "runs": trials["runs"],
                           "snapshot_sha256": register_sha256,
                           "through_record_sha256": records[-1]["sha256"]},
        "holdout": {
            "net_return": _number((run.equity_curve[-1] - STARTING_CAPITAL) / STARTING_CAPITAL),
            "max_drawdown": _number(maximum_drawdown(run.equity_curve)),
            "observations": len(run.returns), "round_trips": len(run.round_trips),
            "cash_charges": str(run.cash_charges), "spread_drag": str(run.spread_drag),
            "slippage_drag": str(run.slippage_drag),
        },
        "buy_and_hold": {
            "net_return": _number((floor.equity_curve[-1] - STARTING_CAPITAL) / STARTING_CAPITAL),
            "observations": len(floor.returns), "cash_charges": str(floor.cash_charges),
        },
        "path_stress": path_stress(run.returns),
        "validation": {"protocol": PROTOCOL, "training": _period(train),
                       "holdout": _period(holdout), "selected_lookback": selected,
                       "training_candidates": training_results,
                       "selection": "highest_training_net_return_then_smallest_lookback"},
        "cost_assumptions": _cost_assumptions(evaluator),
        "limitations": _limitations(train, holdout, trials["candidate_trials"], run, floor),
        "traded_configuration_differences": list(TRADED_CONFIGURATION_DIFFERENCES),
        "sibling_panels": SIBLING_PANELS,
        "automatic_promotion": False, "is_the_traded_ai": False,
        "status": "historical_research_only",
    }
    validate_report(report)
    report["report_sha256"] = _sha(_canonical(report))
    document = _canonical(report)
    _require(len(document) <= MAX_REPORT_BYTES, "research_report_too_large")
    _require(register.is_file() and _sha(register.read_bytes()) == register_sha256,
             "research_trial_register_changed_during_publication")
    _atomic_write(output, document)
    return report


def publish_from_args(args: Any) -> int:
    """Strict declared identity: never substitute a symbol for an unlabelled CSV."""
    try:
        _require(bool(args.data), "publish-research requires --data")
        source = Path(args.data).expanduser().resolve()
        _require(source.suffix.lower() == ".json", "research_declared_json_dataset_required")
        original = source.read_bytes()
        payload = json.loads(original)
        provenance = payload.get("provenance", {})
        _require(
            provenance.get("price_series") == "raw_quote_unadjusted_close"
            and provenance.get("adjusted_close_used") is False
            and provenance.get("interval") == "1d",
            "research_raw_daily_price_provenance_required",
        )
        instrument = dataset_instrument(source)
        _require(instrument is not None, "research_declared_instrument_required")
        if args.market is not None:
            requested = Market.INDIA if args.market == "india" else Market.USA
            _require(requested == instrument.market, "research_market_contradicts_dataset")
        all_bars = load_replay_dataset(source, instrument).bars
        _assert_daily_bars(all_bars)
        venue = venue_for(instrument.market)
        start = date.fromisoformat(args.start) if args.start else None
        end = date.fromisoformat(args.end) if args.end else None
        _require(not (start and end and start > end), "research_date_range_reversed")
        bars = tuple(b for b in all_bars
                     if (start is None or session_date(b.timestamp, venue) >= start)
                     and (end is None or session_date(b.timestamp, venue) <= end))
        _require(bool(bars), "research_no_bars_in_range")
        _require(source.read_bytes() == original, "research_dataset_changed_during_load")
        configured = os.environ.get("PRAMANA_RESEARCH_REPORT", "").strip()
        output = (Path(configured).expanduser().resolve() if configured
                  else paths.ledger_path("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB").parent
                  / "research-report.json")
        register = paths.trial_register("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
        _require(output.resolve() != source, "research_output_overwrites_dataset")
        _require(register.resolve() != source, "research_register_overwrites_dataset")
        report = publish_research(
            bars, instrument=instrument, register=register,
            output=output, source_file_sha256=_sha(original),
        )
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        # Do not echo input contents, environment values, credentials or a traceback.
        reason = str(error) if isinstance(error, ResearchPublicationRefused) else type(error).__name__
        raise SystemExit(f"research publication refused: {reason}") from None
    print(json.dumps({"status": "published", "path": str(output),
                      "report_sha256": report["report_sha256"],
                      "candidate_trials": report["candidate_trials"]}, allow_nan=False))
    return 0
