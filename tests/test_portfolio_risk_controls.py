"""Cross-position risk: the measure, the warden gates and the book stress veto."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.directives import FounderDirectives
from quant_ai.intelligence.adversarial import AdversarialStressAgent, StressScenario
from quant_ai.marketdata.models import Candle
from quant_ai.notifications.trading import TradingNotificationDispatcher
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.book_history import DailyCloseHistory, sector_map_from_env
from quant_ai.risk.policy import BookRiskFirewall, BookRiskPolicy
from quant_ai.risk.portfolio_risk import (
    BookPosition,
    align_daily_closes,
    correlation_adjusted_gross,
    measure_book_risk,
)
from quant_ai.risk.warden import RiskWarden

EQUITY = Decimal(100000)
UP = Decimal("1.25")  # +25% exactly
DOWN = Decimal("0.8")  # -20% exactly
DOUBLE = Decimal(2)  # +100% exactly
HALVE = Decimal("0.5")  # -50% exactly

# Two exactly-representable return patterns. Every close is a product of 1.25/0.8
# powers around 100, so the returns the measure recomputes are exact decimals and
# every expectation below is arithmetic, not a tolerance.
ALTERNATING = [UP, DOWN] * 50  # period 2: + - + -
PAIRED = [UP, UP, DOWN, DOWN] * 25  # period 4: + + - -
RATES_SCENARIO = StressScenario("YIELDS_PLUS_50BPS", Decimal("-0.035"), 50)


def closes(multipliers, start: Decimal = Decimal(100)) -> tuple[Decimal, ...]:
    level = start
    values = [level]
    for multiplier in multipliers:
        level = level * multiplier
        values.append(level)
    return tuple(values)


def dated(values) -> tuple[tuple[str, Decimal], ...]:
    first = date(2026, 1, 1)
    return tuple(
        ((first + timedelta(days=index)).isoformat(), value)
        for index, value in enumerate(values)
    )


def history(**series):
    rows = {symbol: dated(values) for symbol, values in series.items()}

    def provider(symbols):
        return {symbol: rows[symbol] for symbol in symbols if symbol in rows}

    return provider


def plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            EQUITY,
            Decimal("0.75"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )


def buy(symbol: str = "BBB", quantity: int = 50) -> TradeProposal:
    return TradeProposal(
        f"decision-{symbol}", symbol, Market.USA, "USA", AssetClass.EQUITY, Side.BUY,
        quantity, Decimal(100), Decimal(95), Decimal(110), Decimal("0.75"),
        Decimal("0.03"), Decimal("0.02"), ("consensus",),
    )


def book(exposure: dict[str, Decimal], **kwargs) -> PortfolioSnapshot:
    gross = sum(exposure.values(), Decimal(0))
    base = {
        "equity": EQUITY,
        "daily_realized_pnl": Decimal(0),
        "gross_exposure": gross,
        "peak_equity": EQUITY,
        "symbol_exposure": dict(exposure),
        "asset_exposure": {AssetClass.EQUITY: gross} if gross else {},
        "country_exposure": {"USA": gross},
    }
    base.update(kwargs)
    return PortfolioSnapshot(**base)


def warden(**kwargs) -> RiskWarden:
    return RiskWarden(
        TradingNotificationDispatcher(sinks=()), book_risk=BookRiskFirewall(**kwargs)
    )


# --------------------------------------------------------------------------- #
# The measure                                                                  #
# --------------------------------------------------------------------------- #


def test_expected_shortfall_and_var_match_a_hand_computed_series() -> None:
    """One position, 100 intervals, four distinct returns, no tolerances.

    Returns: 48 x +25%, 48 x -20%, 2 x +100%, 2 x -50%. On a 1,000 position the
    signed losses (-pnl) are 48 x -250, 48 x +200, 2 x -1,000 and 2 x +500.
    Ascending they are [-1000 x2, -250 x48, +200 x48, +500 x2].

    95% VaR is the nearest-rank 95th percentile: index ceil(0.95 * 100) - 1 = 94,
    which is +200. The 5% tail is 100 * 0.05 = 5 whole observations, the worst
    five being 500, 500, 200, 200, 200, so expected shortfall is 1,600 / 5 = 320.
    """
    position = BookPosition(
        "AAA", Decimal(1000), closes([UP, DOWN] * 48 + [DOUBLE, HALVE] * 2)
    )
    measure = measure_book_risk([position], Decimal(1000))

    assert measure.available
    assert measure.intervals == 100
    assert measure.value_at_risk_95 == Decimal(200)
    assert measure.expected_shortfall_95 == Decimal(320)
    assert measure.expected_shortfall_fraction == Decimal("0.32")
    assert measure.value_at_risk_fraction == Decimal("0.2")


def test_expected_shortfall_integrates_the_fractional_tail_boundary() -> None:
    """104 intervals put a fraction of an observation on the tail boundary.

    Returns: 48 x +25%, 48 x -20%, 4 x +100%, 4 x -50%. Ascending signed losses
    on a 1,000 position are [-1000 x4, -250 x48, +200 x48, +500 x4]. The tail
    mass is 104 * 0.05 = 5.2, so the worst five observations (500, 500, 500, 500,
    200) enter whole and the sixth (200) enters with weight 0.2:
    (2,200 + 40) / 5.2.
    """
    position = BookPosition(
        "AAA", Decimal(1000), closes([UP, DOWN] * 48 + [DOUBLE, HALVE] * 4)
    )
    measure = measure_book_risk([position], Decimal(1000))

    assert measure.intervals == 104
    assert measure.expected_shortfall_95 == Decimal(2240) / Decimal("5.2")


def test_sample_covariance_and_correlation_follow_the_dashboard_convention() -> None:
    """Centred returns with an n-1 denominator, and exact 1 / 0 correlations."""
    identical = closes(ALTERNATING)
    orthogonal = closes(PAIRED)
    measure = measure_book_risk(
        [
            BookPosition("AAA", Decimal(5000), identical),
            BookPosition("BBB", Decimal(5000), identical),
            BookPosition("CCC", Decimal(5000), orthogonal),
        ],
        EQUITY,
    )

    assert measure.available
    # Returns alternate +0.25 / -0.20 in equal counts, so every centred return is
    # +/-0.225 and the sample variance is 100 * 0.225^2 / 99.
    assert measure.covariance[0][0] == Decimal(100) * Decimal("0.050625") / Decimal(99)
    assert measure.correlation_of("AAA", "BBB") == Decimal(1)
    assert measure.correlation_of("AAA", "CCC") == Decimal(0)
    assert measure.weight_of("AAA") == Decimal("0.05")
    assert measure.diversification_ratio is not None


def test_correlation_adjusted_gross_spans_the_sum_and_the_diversified_equivalent() -> None:
    """Perfect correlation counts the sum; independence counts sqrt(sum of squares)."""
    identical = closes(ALTERNATING)
    orthogonal = closes(PAIRED)
    together = measure_book_risk(
        [
            BookPosition("AAA", Decimal(5000), identical),
            BookPosition("BBB", Decimal(5000), identical),
        ],
        EQUITY,
    )
    apart = measure_book_risk(
        [
            BookPosition("AAA", Decimal(5000), identical),
            BookPosition("BBB", Decimal(5000), orthogonal),
        ],
        EQUITY,
    )

    assert correlation_adjusted_gross(together) == Decimal("0.1")
    assert correlation_adjusted_gross(apart) == (Decimal("0.005")).sqrt()
    assert correlation_adjusted_gross(apart) < correlation_adjusted_gross(together)


def test_negative_correlation_is_floored_rather_than_credited_as_a_hedge() -> None:
    """A sample hedge may not shrink the measure below the independent case."""
    up_first = closes(ALTERNATING)
    down_first = closes([DOWN, UP] * 50)
    measure = measure_book_risk(
        [
            BookPosition("AAA", Decimal(5000), up_first),
            BookPosition("BBB", Decimal(5000), down_first),
        ],
        EQUITY,
    )

    assert measure.correlation_of("AAA", "BBB") == Decimal(-1)
    assert correlation_adjusted_gross(measure) == (Decimal("0.005")).sqrt()


def test_insufficient_history_is_an_explicit_unavailable_not_a_number() -> None:
    short = measure_book_risk(
        [BookPosition("AAA", Decimal(5000), closes([UP, DOWN] * 29))], EQUITY
    )
    assert not short.available
    assert short.reason == "insufficient_history:58_of_60_intervals"
    assert short.expected_shortfall_95 is None
    assert short.daily_volatility is None

    covariance_only = measure_book_risk(
        [BookPosition("AAA", Decimal(5000), closes([UP, DOWN] * 40))], EQUITY
    )
    assert covariance_only.available
    assert covariance_only.expected_shortfall_95 is None
    assert covariance_only.tail_reason == "insufficient_tail_history:80_of_100_intervals"


def test_malformed_books_are_measured_as_unavailable_rather_than_raising() -> None:
    good = closes(ALTERNATING)
    cases = {
        "invalid_equity": ([BookPosition("AAA", Decimal(1), good)], Decimal(0)),
        "no_invested_positions": ([], EQUITY),
        "misaligned_history": (
            [
                BookPosition("AAA", Decimal(1), good),
                BookPosition("BBB", Decimal(1), good[:-1]),
            ],
            EQUITY,
        ),
        "duplicate_position_identity": (
            [BookPosition("AAA", Decimal(1), good), BookPosition("AAA", Decimal(1), good)],
            EQUITY,
        ),
        "invalid_close:AAA": (
            [BookPosition("AAA", Decimal(1), (Decimal(0),) + good[1:])],
            EQUITY,
        ),
        "invalid_market_value:AAA": ([BookPosition("AAA", Decimal(-1), good)], EQUITY),
    }
    for reason, (positions, equity) in cases.items():
        measure = measure_book_risk(positions, equity)
        assert not measure.available, reason
        assert measure.reason == reason


def test_alignment_withholds_a_book_with_a_missing_session_close() -> None:
    full = dated(closes(ALTERNATING))
    aligned, reason = align_daily_closes({"AAA": full, "BBB": full})
    assert reason == ""
    assert len(aligned["AAA"]) == 101

    gapped, reason = align_daily_closes({"AAA": full, "BBB": full[:40] + full[41:]})
    assert gapped == {}
    assert reason == "missing_session_closes:BBB:1"

    unordered, reason = align_daily_closes({"AAA": (full[1], full[0])})
    assert unordered == {}
    assert reason == "unordered_session_closes:AAA"


# --------------------------------------------------------------------------- #
# The warden gates                                                             #
# --------------------------------------------------------------------------- #

LOOSE = BookRiskPolicy(
    max_correlation_adjusted_gross=Decimal("0.08"),
    max_book_expected_shortfall=Decimal(1),
)


def test_second_correlated_entry_is_refused_where_an_uncorrelated_one_is_allowed() -> None:
    """Two 5% positions: one bet of 10% when correlated, 7.07% when not.

    Both books pass every notional bucket the engine had before - 5% single
    trade, 10% symbol, 40% asset class, 60% gross - and are indistinguishable to
    all of them.
    """
    held = book({"AAA": Decimal(5000)}, symbol_quantity={"AAA": 50})
    identical = closes(ALTERNATING)
    orthogonal = closes(PAIRED)

    correlated = warden(
        policy=LOOSE, history_provider=history(AAA=identical, BBB=identical)
    ).evaluate(buy(), plan(), held)
    diversified = warden(
        policy=LOOSE, history_provider=history(AAA=identical, BBB=orthogonal)
    ).evaluate(buy(), plan(), held)

    assert not correlated.approved
    assert correlated.reason == "correlation_adjusted_gross_limit"
    assert diversified.approved, diversified.reason


def test_correlation_gate_dispatches_the_rejection_to_the_operator() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    identical = closes(ALTERNATING)
    decision = RiskWarden(
        dispatcher,
        book_risk=BookRiskFirewall(
            LOOSE, history_provider=history(AAA=identical, BBB=identical)
        ),
    ).evaluate(
        buy(), plan(), book({"AAA": Decimal(5000)}, symbol_quantity={"AAA": 50}),
        tenant_id="t1",
    )

    assert not decision.approved
    assert dispatcher.pending("t1")[-1].metadata["reason"] == "correlation_adjusted_gross_limit"


def test_sector_limit_blocks_a_third_same_sector_entry() -> None:
    """Group concentration needs no history: the operator's mapping arms it."""
    held = book(
        {"AAA": Decimal(5000), "BBB": Decimal(5000)},
        symbol_quantity={"AAA": 50, "BBB": 50},
    )
    policy = BookRiskPolicy(max_sector_exposure=Decimal("0.12"))
    grouped = warden(
        policy=policy, sector_map={"AAA": "IT", "BBB": "IT", "CCC": "IT"}
    ).evaluate(buy("CCC"), plan(), held)
    separate = warden(
        policy=policy, sector_map={"AAA": "IT", "BBB": "IT", "CCC": "PHARMA"}
    ).evaluate(buy("CCC"), plan(), held)

    assert not grouped.approved
    assert grouped.reason == "sector_concentration_limit:IT"
    assert separate.approved, separate.reason


def test_unmapped_symbols_are_ungrouped_rather_than_grouped_by_a_guess() -> None:
    held = book(
        {"AAA": Decimal(5000), "BBB": Decimal(5000)},
        symbol_quantity={"AAA": 50, "BBB": 50},
    )
    decision = warden(
        policy=BookRiskPolicy(max_sector_exposure=Decimal("0.12")),
        sector_map={"ZZZ": "IT"},
    ).evaluate(buy("CCC"), plan(), held)

    assert decision.approved, decision.reason


def test_book_expected_shortfall_limit_blocks_an_entry_it_cannot_afford() -> None:
    """One 5% position whose worst 5% of days average a 1,000 loss on 100,000.

    Returns alternate +25% / -20%, so on a 5,000 position the signed losses are
    50 x -1,250 and 50 x +1,000. The five worst are all 1,000, so expected
    shortfall is 1,000 - exactly 1% of equity.
    """
    provider = history(BBB=closes(ALTERNATING))
    flat = book({})
    measured = warden(
        policy=BookRiskPolicy(max_book_expected_shortfall=Decimal("0.02")),
        history_provider=provider,
    ).evaluate(buy(), plan(), flat)
    refused = warden(
        policy=BookRiskPolicy(max_book_expected_shortfall=Decimal("0.009")),
        history_provider=provider,
    ).evaluate(buy(), plan(), flat)

    assert measured.approved, measured.reason
    assert not refused.approved
    assert refused.reason == "book_expected_shortfall_limit"


def test_insufficient_history_fails_closed() -> None:
    """An armed measure that cannot be computed refuses the entry."""
    decision = warden(history_provider=history(BBB=closes([UP, DOWN] * 20))).evaluate(
        buy(), plan(), book({})
    )
    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:insufficient_history:40_of_60_intervals"

    no_tail = warden(history_provider=history(BBB=closes([UP, DOWN] * 40))).evaluate(
        buy(), plan(), book({})
    )
    assert not no_tail.approved
    assert no_tail.reason == (
        "book_risk_measure_unavailable:insufficient_tail_history:80_of_100_intervals"
    )


def test_a_symbol_without_history_fails_closed() -> None:
    decision = warden(
        history_provider=history(AAA=closes(ALTERNATING))
    ).evaluate(buy(), plan(), book({"AAA": Decimal(5000)}, symbol_quantity={"AAA": 50}))

    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:no_history:BBB"


def test_a_failing_history_source_fails_closed_without_raising() -> None:
    """A measurement fault blocks the entry and never reaches the cadence."""

    def broken(symbols):
        raise RuntimeError("provider down")

    decision = warden(history_provider=broken).evaluate(buy(), plan(), book({}))

    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:RuntimeError"


def test_an_unarmed_warden_leaves_the_entry_path_exactly_as_it_was() -> None:
    assert RiskWarden().book_risk_armed == ()
    decision = RiskWarden(TradingNotificationDispatcher(sinks=())).evaluate(
        buy(), plan(), book({"AAA": Decimal(5000)}, symbol_quantity={"AAA": 50})
    )
    assert decision.approved
    assert decision.reason == "approved"


def test_risk_reducing_sells_pass_every_new_gate() -> None:
    """Every armed gate is set to refuse, and the covered exit still goes through."""

    def broken(symbols):
        raise RuntimeError("provider down")

    held = book(
        {"AAA": Decimal(5000), "BBB": Decimal(5000)},
        symbol_quantity={"AAA": 50, "BBB": 50},
    )
    exit_order = TradeProposal(
        "exit", "AAA", Market.USA, "USA", AssetClass.EQUITY, Side.SELL, 50,
        Decimal(100), Decimal(105), Decimal(90), Decimal(1), Decimal(0), Decimal(0),
        ("de-risk",),
    )
    gate = warden(
        policy=BookRiskPolicy(
            max_correlation_adjusted_gross=Decimal("0.0001"),
            max_sector_exposure=Decimal("0.0001"),
            max_book_expected_shortfall=Decimal("0.0001"),
        ),
        history_provider=broken,
        sector_map={"AAA": "IT", "BBB": "IT"},
    )

    decision = gate.evaluate(exit_order, plan(), held)

    assert decision.approved, decision.reason
    assert decision.reason == "approved"
    assert decision.order is not None and decision.order.side == Side.SELL
    # A partial exit is still a pure unwind and must be treated the same way.
    partial = gate.evaluate(
        TradeProposal(
            "partial", "AAA", Market.USA, "USA", AssetClass.EQUITY, Side.SELL, 20,
            Decimal(100), Decimal(105), Decimal(90), Decimal(1), Decimal(0), Decimal(0),
            ("de-risk",),
        ),
        plan(),
        held,
    )
    assert partial.approved, partial.reason


def test_book_gates_report_which_inputs_an_operator_supplied() -> None:
    armed = warden(history_provider=history(), sector_map={"AAA": "IT"})
    assert armed.book_risk_armed == ("return_history", "sector_map")
    assert armed.book_risk_policy.max_correlation_adjusted_gross == Decimal("0.45")


def test_book_risk_policy_rejects_nonsensical_thresholds() -> None:
    with pytest.raises(ValueError):
        BookRiskPolicy(max_sector_exposure=Decimal(0))
    with pytest.raises(ValueError):
        BookRiskPolicy(correlation_floor=Decimal(2))


# --------------------------------------------------------------------------- #
# Operator-supplied grouping                                                   #
# --------------------------------------------------------------------------- #


def test_sector_map_comes_from_directives_or_the_environment_and_never_a_guess(
    monkeypatch, tmp_path
) -> None:
    assert FounderDirectives().sector_map == {}
    assert FounderDirectives.from_json({"sector_map": {" tcs ": " it services "}}).sector_map == {
        "TCS": "IT SERVICES"
    }

    monkeypatch.delenv("PRAMANA_SECTOR_MAP_JSON", raising=False)
    monkeypatch.delenv("PRAMANA_SECTOR_MAP_FILE", raising=False)
    assert sector_map_from_env() == {}

    monkeypatch.setenv("PRAMANA_SECTOR_MAP_JSON", '{"infy": "it_services"}')
    assert sector_map_from_env() == {"INFY": "IT_SERVICES"}

    monkeypatch.delenv("PRAMANA_SECTOR_MAP_JSON")
    location = tmp_path / "sectors.json"
    location.write_text('{"hdfcbank": "banks"}', encoding="utf-8")
    monkeypatch.setenv("PRAMANA_SECTOR_MAP_FILE", str(location))
    assert sector_map_from_env() == {"HDFCBANK": "BANKS"}


# --------------------------------------------------------------------------- #
# The book stress veto                                                         #
# --------------------------------------------------------------------------- #


def test_book_stress_vetoes_where_a_single_trade_stress_would_not() -> None:
    """A 5% entry onto a 25% book is a 2.4% crisis-gap loss; alone it is 0.4%."""
    agent = AdversarialStressAgent()
    onto_a_book = agent.evaluate(buy(), book({"AAA": Decimal(25000)}))
    onto_nothing = agent.evaluate(buy(), book({}))

    assert not onto_a_book.passed
    assert onto_a_book.flags == ("BOOK_STRESS_VETO",)
    assert onto_a_book.book_worst_scenario == "CRISIS_GAP_DOWN_8PCT"
    assert onto_a_book.book_loss_fraction_of_equity == Decimal("0.024")
    # The single-trade measure the veto used to rely on sees only 0.4%.
    assert onto_a_book.loss_fraction_of_equity == Decimal("0.004")
    assert onto_nothing.passed


def test_the_single_trade_stress_rule_is_unchanged() -> None:
    """The old rule still fires exactly where it fired, so nothing is loosened."""
    agent = AdversarialStressAgent()
    at_tolerance = agent.evaluate(buy(quantity=125), book({}))
    over_tolerance = agent.evaluate(buy(quantity=130), book({}))

    assert at_tolerance.passed
    assert at_tolerance.loss_fraction_of_equity == Decimal("0.01")
    assert not over_tolerance.passed
    assert over_tolerance.flags == ("STRESS_VETO",)
    assert over_tolerance.worst_scenario == "CRISIS_GAP_DOWN_8PCT"


def test_the_book_loss_three_positions_hide_is_computed_and_configurable() -> None:
    """Three 4.5% positions gap together for 1.08% of equity.

    The default 2% tolerance permits it - five positions at the 5% cap is the
    largest book the default position count can hold, and that book is exactly
    at tolerance. An operator who wants it refused has the number in front of
    them on every verdict.
    """
    held = book({"AAA": Decimal(4500), "BBB": Decimal(4500)})
    candidate = buy("CCC", quantity=45)

    default = AdversarialStressAgent().evaluate(candidate, held)
    assert default.passed
    assert default.book_loss_fraction_of_equity == Decimal("0.0108")

    strict = AdversarialStressAgent(book_tolerance_fraction=Decimal("0.01"))
    assert not strict.evaluate(candidate, held).passed

    # The tolerance binds at 25% gross under the worst default scenario.
    at_limit = AdversarialStressAgent().evaluate(buy(), book({"AAA": Decimal(20000)}))
    assert at_limit.passed
    assert at_limit.book_loss_fraction_of_equity == Decimal("0.02")


def test_a_covered_exit_shrinks_the_stressed_book() -> None:
    held = book({"AAA": Decimal(25000)}, symbol_quantity={"AAA": 250})
    exit_order = TradeProposal(
        "exit", "AAA", Market.USA, "USA", AssetClass.EQUITY, Side.SELL, 250,
        Decimal(100), Decimal(105), Decimal(90), Decimal(1), Decimal(0), Decimal(0),
        ("de-risk",),
    )
    verdict = AdversarialStressAgent().evaluate(exit_order, held)

    assert verdict.book_loss == Decimal(0)
    assert verdict.passed


def test_bond_exposure_is_stressed_on_the_rates_leg() -> None:
    """A five-year duration on a 50bp move, applied to the book, not one trade.

    The asset breakdown on the snapshot is what makes this possible: the old
    measure could only apply the rates leg to the proposed order, so 40,000 of
    held gilts were shocked at nothing at all.
    """
    held = PortfolioSnapshot(
        EQUITY, Decimal(0), Decimal(40000), EQUITY,
        symbol_exposure={"GILT": Decimal(40000)},
        asset_exposure={AssetClass.BOND: Decimal(40000)},
    )
    proposal = TradeProposal(
        "add", "GILT2", Market.USA, "USA", AssetClass.BOND, Side.BUY, 50,
        Decimal(100), Decimal(95), Decimal(110), Decimal(1), Decimal(0), Decimal(0),
        ("add",),
    )
    rates_only = (RATES_SCENARIO,)
    verdict = AdversarialStressAgent().evaluate(proposal, held, rates_only)

    # 45,000 of bonds at a five-year duration on 50bp is 2.5%, so 1,125.
    assert verdict.book_worst_scenario == "YIELDS_PLUS_50BPS"
    assert verdict.book_loss == Decimal("1125.000")
    # The single-trade measure sees only the 5,000 the order adds.
    assert verdict.projected_loss == Decimal("125.000")


def test_gross_exposure_without_an_asset_breakdown_is_still_stressed() -> None:
    held = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(25000), EQUITY)
    verdict = AdversarialStressAgent().evaluate(buy(), held)

    assert not verdict.passed
    assert verdict.book_loss_fraction_of_equity == Decimal("0.024")


def test_stress_tolerances_must_be_fractions() -> None:
    with pytest.raises(ValueError):
        AdversarialStressAgent(book_tolerance_fraction=Decimal(0))
    with pytest.raises(ValueError):
        AdversarialStressAgent(Decimal(2))


# --------------------------------------------------------------------------- #
# The governed path                                                            #
# --------------------------------------------------------------------------- #


class FixedCIO:
    def __init__(self, proposal: TradeProposal) -> None:
        self.proposal = proposal

    def propose(self, request, evidence, **kwargs):
        return self.proposal


def entry_order() -> OrderIntent:
    return OrderIntent(
        "AAA", Market.USA, Side.BUY, 250, Decimal(100), "seed", AssetClass.EQUITY,
        "default", Decimal(95), Decimal(120),
    )


def test_book_stress_refuses_an_entry_through_the_governed_runtime(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "book.db", starting_capital=EQUITY, slippage_bps=Decimal(0)
    )
    broker.buy(entry_order())
    service = SwarmPaperTradingService(
        cio=FixedCIO(buy()), broker=broker, xai_logger=XAITraceLogger()
    )
    request = AgentAnalysisRequest(
        "BBB", Market.USA, AssetClass.EQUITY,
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc), {}, 0,
    )
    result = service.execute(
        request, (), plan(), book({"AAA": Decimal(25000)}), quantity=50,
        reference_price=Decimal(100), stop_price=Decimal(95),
        take_profit_price=Decimal(110), country="USA",
    )

    assert result.risk_decision.reason == "STRESS_VETO"
    assert result.stress_verdict.flags == ("BOOK_STRESS_VETO",)
    assert result.fill is None


def test_a_covered_exit_still_fills_with_every_new_control_armed(tmp_path) -> None:
    def broken(symbols):
        raise RuntimeError("provider down")

    broker = PaperBrokerService(
        tmp_path / "exit.db", starting_capital=EQUITY, slippage_bps=Decimal(0)
    )
    broker.buy(entry_order())
    exit_proposal = TradeProposal(
        "covered-exit", "AAA", Market.USA, "USA", AssetClass.EQUITY, Side.SELL, 250,
        Decimal(100), Decimal(105), Decimal(90), Decimal(1), Decimal(0), Decimal(0),
        ("de-risk",),
    )
    service = SwarmPaperTradingService(
        cio=FixedCIO(exit_proposal),
        broker=broker,
        xai_logger=XAITraceLogger(),
        warden=warden(
            policy=BookRiskPolicy(
                max_correlation_adjusted_gross=Decimal("0.0001"),
                max_sector_exposure=Decimal("0.0001"),
                max_book_expected_shortfall=Decimal("0.0001"),
            ),
            history_provider=broken,
            sector_map={"AAA": "IT"},
        ),
        stress_agent=AdversarialStressAgent(
            Decimal("0.0001"), book_tolerance_fraction=Decimal("0.0001")
        ),
    )
    request = AgentAnalysisRequest(
        "AAA", Market.USA, AssetClass.EQUITY,
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc), {}, 0,
    )
    result = service.execute(
        request, (), plan(), book({"AAA": Decimal(25000)}, symbol_quantity={"AAA": 250}),
        quantity=250, reference_price=Decimal(100), stop_price=Decimal(105),
        take_profit_price=Decimal(90), country="USA",
    )

    assert result.risk_decision.approved, result.risk_decision.reason
    assert result.fill is not None


def test_the_new_veto_never_permits_what_the_old_one_refused() -> None:
    """Regression guard on the recalibration: the veto set only ever grows.

    The rule the agent shipped with vetoed when the proposed trade alone lost
    more than 1% of equity in the worst scenario. That rule is preserved
    verbatim, so across the whole reachable grid of trade sizes and existing
    books there is no combination the old agent refused and the new one allows.
    """
    agent = AdversarialStressAgent()
    worst = max(-scenario.equity_shock for scenario in agent.DEFAULT_SCENARIOS)
    for quantity in range(10, 400, 10):
        for held in range(0, 60000, 5000):
            verdict = agent.evaluate(
                buy(quantity=quantity), book({"AAA": Decimal(held)} if held else {})
            )
            old_rule_vetoes = (
                Decimal(quantity) * Decimal(100) * worst / EQUITY > Decimal("0.01")
            )
            if old_rule_vetoes:
                assert not verdict.passed, (quantity, held)


# --------------------------------------------------------------------------- #
# Sourcing daily closes from the engine's own bars                             #
# --------------------------------------------------------------------------- #


def candle(instrument: Instrument, when: datetime, close: Decimal) -> Candle:
    return Candle(instrument, when, close, close, close, close, Decimal(1))


class StubDailyHistory:
    def __init__(self, bars: dict[str, tuple[Candle, ...]]) -> None:
        self.bars = bars
        self.requested: list[str] = []

    def fetch(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        self.requested.append(instrument.symbol)
        return self.bars.get(instrument.symbol, ())


def test_daily_closes_align_across_venues_by_trading_date() -> None:
    """An NSE close at 10:00 UTC and a NASDAQ close at 21:00 UTC are one session."""
    infy = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    aapl = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    days = [datetime(2026, 3, 2, tzinfo=timezone.utc) + timedelta(days=i) for i in range(3)]
    provider = StubDailyHistory(
        {
            "INFY": tuple(
                candle(infy, day.replace(hour=10), Decimal(100) + index)
                for index, day in enumerate(days)
            ),
            "AAPL": tuple(
                candle(aapl, day.replace(hour=21), Decimal(200) + index)
                for index, day in enumerate(days)
            ),
        }
    )
    source = DailyCloseHistory(provider, (infy, aapl), lambda: days[-1])

    series = source(["INFY", "AAPL"])

    assert [key for key, _ in series["INFY"]] == [key for key, _ in series["AAPL"]]
    assert [key for key, _ in series["INFY"]] == ["2026-03-02", "2026-03-03", "2026-03-04"]
    assert series["AAPL"][0][1] == Decimal(200)


def test_a_symbol_the_operator_never_configured_yields_no_series() -> None:
    infy = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    when = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
    provider = StubDailyHistory({"INFY": (candle(infy, when, Decimal(100)),)})
    source = DailyCloseHistory(provider, (infy,), lambda: when)

    assert source(["INFY", "GHOST"]) == {"INFY": (("2026-03-02", Decimal(100)),)}
    assert provider.requested == ["INFY"]


def test_an_abstaining_history_provider_yields_no_series_and_fails_closed() -> None:
    infy = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    when = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
    source = DailyCloseHistory(StubDailyHistory({}), (infy,), lambda: when)

    assert source(["INFY"]) == {}
    decision = warden(history_provider=source).evaluate(buy("INFY"), plan(), book({}))
    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:no_history_supplied"
