from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from quant_ai.agents.swarm import TradeProposal
from quant_ai.agents.traded_runtime import build_traded_runtime
from quant_ai.analytics.metrics import (
    MINIMUM_RATIO_OBSERVATIONS,
    MINIMUM_SIGNIFICANCE_OBSERVATIONS,
    mean_return_significance,
    summarize_performance,
)
from quant_ai.backtesting.baselines import (
    BaselineEvaluator,
    default_baselines,
    format_comparison,
)
from quant_ai.backtesting.contest import (
    NOT_THE_AI,
    contest,
    format_contest,
)
from quant_ai.backtesting.replay import (
    TRADED_CONFIGURATION_DIFFERENCES,
    HistoricalReplayDataset,
    HistoricalReplayHarness,
    dataset_instrument,
    load_replay_dataset,
)
from quant_ai.backtesting.tearsheet import build_tearsheet
from quant_ai.config import paths
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.daemon import OPERATOR_HALT_PREFIX, AutonomousTradingDaemon
from quant_ai.execution.notifications import (
    ConsoleNotificationAdapter,
    TradingNotificationDispatcher,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.risk_state import SQLiteRiskStateStore
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import intraday_periods_per_year
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.event_calendar import event_calendar_from_env
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.operations.evidence_log import append_record
from quant_ai.operations.zerodha_login import run_login
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.validation.trial_register import record_trials, register_summary


def build_runtime() -> AutonomousTradingDaemon:
    database = paths.ledger_path("QUANT_AI_PAPER_DB")
    tenant_id = paths.tenant_id("QUANT_AI_TENANT_ID")
    database.parent.mkdir(parents=True, exist_ok=True)
    directives = FounderDirectives.from_env() or FounderDirectives()
    broker = PaperBrokerService(str(database), starting_capital=directives.starting_capital)
    feed = UsaSandboxMarketDataFeed()
    xai_dir = str(paths.proof_directory("PRAMANA_XAI_DIR", "QUANT_AI_XAI_DIR"))
    # Sandbox runtime: no return history source, so only the operator's group limit
    # arms here. Correlation/expected-shortfall stay unarmed.
    # The pilot daemon rebuilds specialist scores from the decision journal at boot
    # (daemon.py). This runtime did not, so ``analytics`` reported an engine that had
    # never loaded anything as an engine that had learned nothing - the same
    # ``agent_attribution=none`` an untraded account prints, and indistinguishable from it.
    runtime = build_traded_runtime(
        broker=broker,
        directives=directives,
        xai_logger=XAITraceLogger(xai_dir, tenant_id=tenant_id),
        attribution_journal_tenant=tenant_id,
    )
    pipeline = SwarmMarketAnalysisPipeline(
        feed,
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=runtime,
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    tracker = PortfolioTracker(broker, feed, tenant_id=tenant_id)
    # The sandbox runtime is US-only; the watchlist applies to the ghost daemon.
    plan = CapitalGoalEngine().recommend(directives.capital_plan_request())
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    notifications = TradingNotificationDispatcher((ConsoleNotificationAdapter(),))
    return AutonomousTradingDaemon(
        scheduler,
        tracker,
        instrument,
        plan,
        # quantity is intentionally unset: each entry is sized from live equity and the
        # capital plan (PositionSizer.quantity_from_plan), not a fixed share count.
        country="USA",
        tenant_id=tenant_id,
        notifications=notifications,
    )


def _portfolio(daemon: AutonomousTradingDaemon) -> None:
    metrics = daemon.tracker.metrics(datetime.now(timezone.utc))
    print(f"cash={metrics.cash_balance}")
    for item in metrics.positions:
        print(
            f"{item.symbol} qty={item.quantity} avg={item.average_entry_price} "
            f"current={item.current_price} unrealized={item.unrealized_pnl}"
        )
    print(f"realized_pnl={metrics.realized_pnl}")
    print(f"unrealized_pnl={metrics.unrealized_pnl}")
    print(f"equity={metrics.total_equity}")
    print(f"drawdown={metrics.drawdown_fraction}")


def _ratio_line(value: Decimal | None) -> str:
    """Print an annualised ratio, or say plainly that the sample cannot support one."""
    if value is None:
        return f"unavailable:fewer_than_{MINIMUM_RATIO_OBSERVATIONS}_observations"
    return str(value)


def _analytics(daemon: AutonomousTradingDaemon) -> None:
    """Statistics of the traded instrument's recent price, not of the paper account.

    Every ratio below is computed from the last hour of one-minute candles for the
    configured instrument. What the account itself did is what ``portfolio`` prints. The
    two used to be indistinguishable here - a bare ``sharpe=`` line in the same operator
    command that prints a bare ``equity=`` line - which is how a number describing one
    stock's last hour gets read as the system's track record. Each line now names the
    series it measures, and the two numbers that cannot be measured at all are withheld.
    """
    now = datetime.now(timezone.utc)
    candles = daemon.scheduler.pipeline.market_feed.fetch_ohlcv(
        daemon.instrument, now - timedelta(minutes=60), now, "1m"
    )
    closes = tuple(item.close for item in candles)
    returns = tuple(
        (after - before) / before
        for before, after in zip(closes, closes[1:])
        if before > 0
    )
    curve = [Decimal(100)]
    for item in returns:
        curve.append(curve[-1] * (Decimal(1) + item))
    periods = intraday_periods_per_year(daemon.instrument.market, timedelta(minutes=1))
    # No benchmark argument. The only series in scope here is the instrument, and
    # regressing it on itself returns alpha=0, beta=1 by construction - a tautology that
    # reads as "tracking the market exactly". Alpha and beta need an independent index, so
    # they are reported as unavailable rather than manufactured from the same column twice.
    metrics = summarize_performance(returns, tuple(curve), returns, periods=periods)
    # The market is part of the name because ``--market`` defaults to ``us``: run without
    # it on an India pilot and every figure below describes AAPL, not anything on the
    # watchlist. That was invisible when the output named no instrument at all.
    print(
        f"series=instrument_price:{daemon.instrument.symbol}:{daemon.instrument.market.value}"
        ":1m:last_60_minutes"
    )
    print("series_measures=instrument_price_not_account_performance")
    # Whether these are real quotes at all. This runtime is served by a sandbox feed
    # whose prices are generated, so the ratios are arithmetic on synthetic input and
    # must never be read as market observation. The account figures that ``portfolio``
    # prints come from the shared paper ledger and are unaffected by this.
    feed = daemon.scheduler.pipeline.market_feed
    sandbox = "Sandbox" in type(feed).__name__
    print(f"series_source={'sandbox_generated_prices' if sandbox else 'live_feed'}")
    # A one-minute series annualised with trading days overstates the ratio by the square
    # root of the bars in a session, so the interval is named alongside every ratio and a
    # sample too short to support one prints no number at all.
    print(f"annualisation_periods_per_year={metrics.periods_per_year}")
    print(f"return_observations={metrics.observations}")
    print(f"instrument_sharpe={_ratio_line(metrics.sharpe)}")
    print(f"instrument_sortino={_ratio_line(metrics.sortino)}")
    # The same sample, unannualised. sharpe == t_statistic * sqrt(periods_per_year / n),
    # so a t-statistic near zero and a large Sharpe are one measurement printed at two
    # scales, not two findings: the t-statistic is the one that says whether the mean
    # return is distinguishable from zero at all.
    significance = mean_return_significance(returns)
    if significance is None:
        print(f"instrument_mean_return_t_statistic=unavailable:fewer_than_{MINIMUM_SIGNIFICANCE_OBSERVATIONS}_observations")
    else:
        print(f"instrument_mean_return_t_statistic={significance.t_statistic}")
        print(f"instrument_mean_return_observations={significance.observations}")
        print("instrument_mean_return_multiple_testing_correction=none")
    print(f"instrument_max_drawdown={metrics.max_drawdown}")
    # Up minutes over down minutes in the price series. Not trades: the account's realised
    # wins and losses are not in this command at all.
    print(f"instrument_up_down_minute_ratio={metrics.win_loss_ratio}")
    print(f"instrument_var_95={metrics.var_95}")
    print("alpha=unavailable:no_independent_benchmark")
    print("beta=unavailable:no_independent_benchmark")
    attribution = daemon.scheduler.pipeline.runtime.attribution.attribution()
    if not attribution:
        print("agent_attribution=none")
    for row in attribution:
        print(
            f"agent={row.agent_id} hit_rate={row.hit_rate} pnl={row.pnl} "
            f"weight={row.conviction_weight}"
        )


def _stress_test(daemon: AutonomousTradingDaemon) -> None:
    metrics = daemon.tracker.metrics(datetime.now(timezone.utc))
    if not metrics.positions:
        print("stress_test=no_open_positions")
        return
    snapshot = daemon.tracker.get_snapshot()
    stress_agent = daemon.scheduler.pipeline.runtime.stress_agent
    for item in metrics.positions:
        proposal = TradeProposal(
            "cli-stress", item.symbol, item.market, daemon.country, item.asset_class,
            Side.BUY, item.quantity, item.current_price, None, None, Decimal(1), Decimal(0), Decimal(0),
            ("current_paper_position_stress_test",),
        )
        verdict = stress_agent.evaluate(proposal, snapshot)
        print(
            f"symbol={item.symbol} passed={verdict.passed} "
            f"scenario={verdict.worst_scenario} projected_loss={verdict.projected_loss} "
            f"equity_loss_fraction={verdict.loss_fraction_of_equity}"
        )


def _friction_audit(daemon: AutonomousTradingDaemon) -> None:
    broker = daemon.tracker.broker
    totals = broker.friction_totals(daemon.tenant_id)
    statutory_codes = {"STT", "EXCHANGE", "SEBI", "GST", "STAMP", "SEC", "FINRA_TAF"}
    statutory = sum((amount for code, amount in totals.items() if code in statutory_codes), Decimal(0))
    print(f"brokerage={totals.get('BROKERAGE', Decimal(0))}")
    print(f"depository={totals.get('DP', Decimal(0))}")
    print(f"statutory_fees={statutory}")
    print(f"slippage={totals.get('SLIPPAGE', Decimal(0))}")
    print(f"spread={totals.get('SPREAD', Decimal(0))}")
    for code in sorted(totals):
        print(f"{code.lower()}={totals[code]}")


def _replay_instrument(market: str | None, data: str | None = None) -> Instrument:
    """What a replay or baseline run is scoring, taken from the dataset wherever it says.

    ``--market`` used to decide this alone, and it resolved an absent flag to the US: an
    NSE series scored without the flag was priced against the US fee schedule and
    annualised against the US session length, and every line of the output - the table, the
    trial-register study, the proof - named AAPL. Nothing said so. Worse, the flag only
    ever chose between two hardcoded instruments, so a run over ``INFY.json`` was recorded
    as RELIANCE even when the flag was right.

    A dataset written by ``scripts/fetch_historical_bars.py`` states its own symbol, market,
    asset class, exchange and currency, and that statement wins. When the dataset declares
    nothing - a hand-written fixture, a CSV - the flag is used, and is now required rather
    than defaulted, because guessing a market silently is the failure above. When both
    exist and disagree, neither is trusted: an operator who has mixed up two files needs to
    be told, not to be handed one of the two answers.
    """
    declared = _declared_instrument(data)
    if declared is not None:
        if market is not None and _requested_market(market) != declared.market:
            raise SystemExit(
                f"--market {market} contradicts {data}, which declares "
                f"{declared.symbol} on {declared.market.value}"
            )
        return declared
    if market is None:
        raise SystemExit(
            f"--market is required: {data or 'this dataset'} does not declare the "
            "instrument it holds, and a market cannot be guessed - it selects the "
            "statutory fee schedule and the session length ratios are annualised against"
        )
    resolved = _requested_market(market)
    return (
        Instrument("RELIANCE", resolved, AssetClass.EQUITY, "INR", "NSE")
        if resolved == Market.INDIA
        else Instrument("AAPL", resolved, AssetClass.EQUITY, "USD", "NASDAQ")
    )


def _requested_market(market: str) -> Market:
    return Market.INDIA if market == "india" else Market.USA


def _declared_instrument(data: str | None) -> Instrument | None:
    """The dataset's own instrument, with a malformed declaration reported as an operator
    error rather than a traceback."""
    if not data:
        return None
    try:
        return dataset_instrument(data)
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(f"{data}: cannot read the declared instrument ({error})") from error


def _windowed_bars(dataset: HistoricalReplayDataset, args: argparse.Namespace, command: str):
    start_date = datetime.fromisoformat(args.start).date() if args.start else None
    end_date = datetime.fromisoformat(args.end).date() if args.end else None
    bars = tuple(
        bar for bar in dataset.bars
        if (start_date is None or bar.timestamp.date() >= start_date)
        and (end_date is None or bar.timestamp.date() <= end_date)
    )
    if not bars:
        raise SystemExit(f"no bars in requested {command} range")
    return bars


def _baselines(args: argparse.Namespace) -> None:
    """Score the deterministic, price-only floors the swarm has to beat.

    Nothing here consults a model, a headline or a fundamental: these are the numbers a
    reader needs before "the AI decided X" can be read as anything. The run is registered
    as five candidate evaluations for the same reason a replay is - a sweep of windows
    must not be reportable as one lucky look.
    """
    if not args.data:
        raise SystemExit("baselines requires --data")
    instrument = _replay_instrument(args.market, args.data)
    bars = _windowed_bars(load_replay_dataset(args.data, instrument), args, "baselines")
    strategies = default_baselines()
    register = paths.trial_register("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
    record_trials(
        register,
        study=f"baselines:{instrument.symbol}:{instrument.market.value}",
        candidate_trials=len(strategies),
        configuration={
            "baselines": [item.baseline_id for item in strategies],
            "bars": len(bars),
            "start": bars[0].timestamp.isoformat(),
            "end": bars[-1].timestamp.isoformat(),
            "market": args.market,
        },
        data_sha256=_bar_digest(bars),
    )
    reports = BaselineEvaluator(instrument=instrument).evaluate(bars, strategies)
    print(format_comparison(reports))
    payload = {
        "schema": "pramana.baseline_comparison.v1",
        "instrument": {"symbol": instrument.symbol, "market": instrument.market.value},
        "bars": len(bars),
        "start": bars[0].timestamp.isoformat(),
        "end": bars[-1].timestamp.isoformat(),
        "registeredTrials": register_summary(register),
        "reports": [item.to_dict() for item in reports],
    }
    proof_dir = paths.proof_directory("PRAMANA_XAI_DIR", "QUANT_AI_XAI_DIR")
    proof_dir.mkdir(parents=True, exist_ok=True)
    document = json.dumps(payload, sort_keys=True, allow_nan=False)
    (proof_dir / "latest-baselines.json").write_text(document)
    print(document)


def _bar_digest(bars) -> str:
    return hashlib.sha256(
        "|".join(
            f"{bar.timestamp.isoformat()}:{bar.open}:{bar.high}:"
            f"{bar.low}:{bar.close}:{bar.volume}"
            for bar in bars
        ).encode()
    ).hexdigest()


def _replayed(args: argparse.Namespace, command: str):
    """Run the replay exactly as ``backtest`` does, and hand back everything it produced.

    Shared rather than copied: ``contest`` puts this curve beside the deterministic floors
    and claims the two faced the same engine. A second wiring of the harness would let the
    two commands drift - a different capital plan, different directives, a different
    database - and the sheet would keep printing, comparing two things that were never the
    same. One builder is the only way that claim stays true.
    """
    if not args.data:
        raise SystemExit(f"{command} requires --data")
    instrument = _replay_instrument(args.market, args.data)
    market = instrument.market
    dataset = load_replay_dataset(args.data, instrument)
    bars = _windowed_bars(dataset, args, command)
    end_time = bars[-1].timestamp
    dataset = HistoricalReplayDataset(
        bars,
        tuple(item for item in dataset.macro if item.observed_at <= end_time),
        tuple(item for item in dataset.news if item.published_at <= end_time),
        tuple(item for item in dataset.fundamentals if item.observed_at <= end_time),
        dataset.benchmark_closes,
        tuple(w for w in dataset.intrabar_windows
              if w.parent_timestamp in {b.timestamp for b in bars[1:]}),
    )
    # Every replay is a look at the data, so the look is recorded before it happens: a
    # sweep of windows cannot be reported as one lucky backtest when the register already
    # counts the runs. The tearsheet then carries the running total.
    register = paths.trial_register("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
    record_trials(
        register,
        # Deliberately not keyed on the command: `contest` is another look at the same
        # data through the same engine, so it counts against the same study. Splitting
        # them would reset a total whose only job is to make a sweep of windows visible.
        study=f"replay:{instrument.symbol}:{instrument.market.value}",
        candidate_trials=1,
        configuration={
            "bars": len(bars),
            "start": bars[0].timestamp.isoformat(),
            "end": bars[-1].timestamp.isoformat(),
            "requested_start": args.start,
            "requested_end": args.end,
            "market": args.market,
        },
        data_sha256=_bar_digest(bars),
    )
    database = os.environ.get("QUANT_AI_BACKTEST_DB", "").strip() or ":memory:"
    if database == "shared":
        database = str(paths.ledger_path())
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    broker = PaperBrokerService(database, starting_capital=Decimal(100000))
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000), Decimal("0.80"), Decimal("0.20"),
            expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
        )
    )
    proof_dir = paths.proof_directory("PRAMANA_XAI_DIR", "QUANT_AI_XAI_DIR")
    proof_dir.mkdir(parents=True, exist_ok=True)
    tenant = paths.tenant_id("QUANT_AI_TENANT_ID")
    result = HistoricalReplayHarness(
        broker,
        plan,
        quantity=None,  # dynamic: sized per bar from equity and the capital plan
        country="India" if market == Market.INDIA else "USA",
        tenant_id=tenant,
        xai_logger=XAITraceLogger(proof_dir, tenant_id=tenant),
        # The founder's own scope and blackout calendar, so the backtested engine is the
        # deployed engine rather than a more permissive relative of it.
        directives=FounderDirectives.from_env() or FounderDirectives(),
        event_calendar=event_calendar_from_env(),
    ).run(dataset)
    return SimpleNamespace(
        instrument=instrument, bars=bars, result=result, broker=broker,
        tenant=tenant, proof_dir=proof_dir, register=register,
    )


def _backtest(args: argparse.Namespace) -> None:
    run = _replayed(args, "backtest")
    trials = register_summary(run.register)
    tearsheet_json = build_tearsheet(
        run.result, run.broker, tenant_id=run.tenant, trial_register=trials
    ).to_json()
    (run.proof_dir / "latest-backtest-tearsheet.json").write_text(tearsheet_json)
    print(tearsheet_json)
    run.broker.flush()


def _contest(args: argparse.Namespace) -> None:
    """Score the replayed rule engine against the floors it has to beat, on one sheet.

    The floors answer "what would owning it, or one dumb rule, have done". This answers
    "did our own deterministic engine do better" - the question that has to come out right
    before the AI's version of it is worth asking, and which no command answered before.
    """
    run = _replayed(args, "contest")
    rows = contest(
        run.bars,
        instrument=run.instrument,
        replay_result=run.result,
        broker=run.broker,
        tenant_id=run.tenant,
    )
    print(format_contest(rows))
    payload = {
        "schema": "pramana.contest.v1",
        "instrument": {
            "symbol": run.instrument.symbol,
            "market": run.instrument.market.value,
        },
        "bars": len(run.bars),
        "start": run.bars[0].timestamp.isoformat(),
        "end": run.bars[-1].timestamp.isoformat(),
        "dataSha256": _bar_digest(run.bars),
        "noneOfTheseIsTheTradedAi": NOT_THE_AI,
        "tradedConfigurationDifferences": list(TRADED_CONFIGURATION_DIFFERENCES),
        "registeredTrials": register_summary(run.register),
        "entrants": [row.to_dict() for row in rows],
    }
    document = json.dumps(payload, sort_keys=True, allow_nan=False)
    (run.proof_dir / "latest-contest.json").write_text(document)
    print(document)
    run.broker.flush()


def _resume(*, clear_fault_halt: bool, operator: str | None) -> int:
    """Release an operator halt. A fault halt needs an explicit, named override.

    ``resume`` removes the halt marker file and clears a persisted halt only when that
    halt came from the marker file. A halt latched by the engine itself - a drawdown
    breach, a failed ledger reconciliation, a failed protective exit, a latched cadence
    fault - is a finding, not a pause, and stays until an operator overrides it by name.
    Every override is printed and appended to a hash-chained override log.
    """
    target = paths.halt_file()
    if target.exists():
        target.unlink()
        print(f"halt released: {target}")
    else:
        print(f"no halt file present: {target}")
    ledger = paths.ledger_path("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
    if not ledger.exists():
        return 0
    tenant = paths.tenant_id("QUANT_AI_TENANT_ID", default="ghost")
    risk_state = SQLiteRiskStateStore(ledger)
    try:
        engaged, reason = risk_state.kill_switch_state(tenant)
        if not engaged:
            print(f"no persisted halt present: tenant={tenant}")
            return 0
        detail = reason or "unrecorded reason"
        if detail.startswith(OPERATOR_HALT_PREFIX):
            risk_state.set_kill_switch(tenant, False, None)
            print(f"persisted operator halt released: tenant={tenant} reason={detail}")
            return 0
        if not clear_fault_halt:
            print(f"refusing to clear fault halt: tenant={tenant} reason={detail}")
            print(
                "This halt was latched by the engine, not by an operator pause. Fix the "
                "cause, then rerun with --clear-fault-halt to override it on the record."
            )
            return 2
        record = append_record(
            paths.halt_override_log("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB"),
            "fault_halt_override",
            {
                "tenant_id": tenant,
                "halt_reason": detail,
                "operator": (operator or "").strip() or "unnamed operator",
                "ledger": str(ledger),
            },
        )
        risk_state.set_kill_switch(tenant, False, None)
        print(f"fault halt cleared by operator override: tenant={tenant} reason={detail}")
        print(f"override recorded: sequence={record['sequence']} sha256={record['sha256']}")
        return 0
    finally:
        risk_state.close()


def _journal_broker() -> tuple[PaperBrokerService, str]:
    """Open the shared ledger for the journal commands without assembling a runtime."""
    ledger = paths.ledger_path("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
    if not ledger.exists():
        raise SystemExit(f"no paper ledger at {ledger}")
    return PaperBrokerService(str(ledger)), paths.tenant_id("QUANT_AI_TENANT_ID", default="ghost")


def _decision_quality(args: argparse.Namespace) -> int:
    from quant_ai.analytics.decision_quality import build_report

    broker, tenant = _journal_broker()
    try:
        report = build_report(
            broker, tenant_id=tenant, now=datetime.now(timezone.utc), since_days=args.since
        )
    finally:
        broker.close()
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


def _post_mortem(args: argparse.Namespace) -> int:
    from quant_ai.analytics.post_mortem import (
        PostMortemApprovedError,
        approve_post_mortem,
        build_post_mortem,
        write_post_mortem,
    )

    directory = paths.post_mortem_directory("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
    now = datetime.now(timezone.utc)
    if args.approve:
        try:
            report = approve_post_mortem(directory, args.approve, now)
        except (FileNotFoundError, ValueError) as error:
            raise SystemExit(str(error)) from error
        print(json.dumps({
            "session_date": report["session_date"], "status": report["status"],
            "approved_at": report["approved_at"], "lessons": len(report.get("lessons", [])),
            "path": str(directory / f"{report['session_date']}.json"),
        }))
        return 0
    broker, tenant = _journal_broker()
    try:
        session = date.fromisoformat(args.date) if args.date else None
        report = build_post_mortem(broker, tenant_id=tenant, now=now, session_date=session)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    finally:
        broker.close()
    try:
        write_post_mortem(directory, report)
    except PostMortemApprovedError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pramana")
    parser.add_argument(
        "command",
        choices=(
            "run-once", "daemon", "portfolio", "analytics", "stress-test",
            "backtest", "baselines", "contest", "publish-research", "friction-audit", "halt", "resume",
            "zerodha-login", "decision-quality", "post-mortem",
        ),
    )
    parser.add_argument("--data")
    parser.add_argument("--start")
    parser.add_argument("--end")
    # No default. A command that cannot honour this flag must refuse it rather than
    # print US sandbox figures under an operator's ``--market india``.
    parser.add_argument("--market", choices=("india", "us"), default=None)
    parser.add_argument("--reason", default="operator halt")
    parser.add_argument(
        "--clear-fault-halt",
        action="store_true",
        help=(
            "resume: also clear a persisted halt the engine latched itself (drawdown, "
            "reconciliation, protective-exit or cadence fault). Prints what it clears and "
            "appends the override to the hash-chained override log."
        ),
    )
    parser.add_argument(
        "--operator", help="resume: name recorded against a --clear-fault-halt override"
    )
    parser.add_argument(
        "--request-token",
        help="zerodha-login: Kite request_token or the full redirect URL; prompted if omitted",
    )
    parser.add_argument("--since", type=int, default=30, help="decision-quality: window in days")
    parser.add_argument("--date", help="post-mortem: IST session date YYYY-MM-DD (default: latest)")
    parser.add_argument(
        "--approve", metavar="YYYY-MM-DD", help="post-mortem: approve the written file for this session"
    )
    args = parser.parse_args(argv)
    if args.command == "decision-quality":
        return _decision_quality(args)
    if args.command == "post-mortem":
        return _post_mortem(args)
    if args.command == "halt":
        target = paths.halt_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.reason.strip() or "operator halt", encoding="utf-8")
        print(f"halt engaged: {target}")
        return 0
    if args.command == "resume":
        return _resume(clear_fault_halt=args.clear_fault_halt, operator=args.operator)
    if args.command == "zerodha-login":
        # Daily Kite token renewal for the paper pilot; never prints or stores secrets.
        return run_login(args.request_token)
    if args.command == "backtest":
        _backtest(args)
        return 0
    if args.command == "publish-research":
        from quant_ai.backtesting.research_publisher import publish_from_args

        return publish_from_args(args)
    if args.command == "contest":
        _contest(args)
        return 0
    if args.command == "baselines":
        _baselines(args)
        return 0
    if args.market is not None:
        raise SystemExit(
            f"--market does not apply to {args.command}: this runtime is a US sandbox. "
            "Only backtest, baselines and contest read --market; the pilot watchlist is set by "
            "founder directives."
        )
    daemon = build_runtime()
    if args.command == "run-once":
        brief = asyncio.run(daemon.run_once())
        print(brief.to_json())
        daemon.tracker.broker.flush()
        return 0
    if args.command == "portfolio":
        _portfolio(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "analytics":
        _analytics(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "stress-test":
        _stress_test(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "friction-audit":
        _friction_audit(daemon)
        daemon.tracker.broker.flush()
        return 0
    asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
