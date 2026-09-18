from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.portfolio.lifecycle import (
    LifecycleAction,
    PositionDirection,
    PositionLifecycleManager,
    PositionLifecyclePolicy,
    PositionLifecycleState,
    ScaleRule,
)

D = Decimal
NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def state(*, direction=PositionDirection.LONG, quantity=100, initial=100, entry="100", extreme=None, stop="95", target=None, completed=frozenset()):
    return PositionLifecycleState(
        "TEST", direction, quantity, initial, D(entry), NOW,
        D(extreme or entry), None if stop is None else D(stop),
        None if target is None else D(target), completed,
    )


def test_protective_stop_and_target_dominate_scaling_rules():
    policy = PositionLifecyclePolicy(
        scale_in_rules=(ScaleRule("add", D("0.01"), D("0.5")),),
        scale_out_rules=(ScaleRule("trim", D("0.01"), D("0.5")),),
    )
    stop = PositionLifecycleManager().evaluate(state(stop="95"), mark_price=D("94"), now=NOW, policy=policy)
    assert stop.action is LifecycleAction.EXIT_ALL and stop.reason == "protective_stop_hit"
    target = PositionLifecycleManager().evaluate(
        state(stop="95", target="105"), mark_price=D("106"), now=NOW, policy=policy
    )
    assert target.action is LifecycleAction.EXIT_ALL and target.reason == "take_profit_hit"


def test_break_even_and_trailing_stop_only_tighten_long_protection():
    policy = PositionLifecyclePolicy(
        break_even_activation_gain=D("0.03"), trailing_activation_gain=D("0.05"),
        trailing_distance_fraction=D("0.02"),
    )
    decision = PositionLifecycleManager().evaluate(
        state(extreme="105", stop="95"), mark_price=D("110"), now=NOW + timedelta(hours=1),
        policy=policy,
    )
    assert decision.action is LifecycleAction.HOLD
    assert decision.proposed_stop_price == D("107.80")
    # Pullback cannot loosen a stop that is already tighter.
    later = PositionLifecycleManager().evaluate(
        state(extreme="110", stop="108"), mark_price=D("106"), now=NOW + timedelta(hours=2),
        policy=policy,
    )
    assert later.proposed_stop_price == D("108")


def test_max_holding_period_forces_exit_even_when_position_is_profitable():
    decision = PositionLifecycleManager().evaluate(
        state(), mark_price=D("104"), now=NOW + timedelta(days=3),
        policy=PositionLifecyclePolicy(max_holding_period=timedelta(days=2)),
    )
    assert decision.action is LifecycleAction.EXIT_ALL
    assert decision.reason == "max_holding_period_reached"


def test_scale_out_is_exact_lot_and_same_rule_never_fires_twice():
    rule = ScaleRule("trim-5pct", D("0.05"), D("0.25"))
    policy = PositionLifecyclePolicy(lot_size=10, scale_out_rules=(rule,))
    first = PositionLifecycleManager().evaluate(
        state(quantity=100, initial=100), mark_price=D("106"), now=NOW, policy=policy
    )
    assert first.action is LifecycleAction.REDUCE and first.quantity == 20
    assert first.completed_rule == rule.rule_id
    second = PositionLifecycleManager().evaluate(
        state(quantity=80, initial=100, completed=frozenset({rule.rule_id})),
        mark_price=D("107"), now=NOW, policy=policy,
    )
    assert second.action is LifecycleAction.HOLD


def test_scale_in_only_adds_to_a_protected_winner_and_respects_position_cap():
    rule = ScaleRule("winner-add", D("0.05"), D("0.20"))
    policy = PositionLifecyclePolicy(
        lot_size=10, scale_in_rules=(rule,), max_position_quantity=120,
        break_even_activation_gain=D("0.03"),
    )
    decision = PositionLifecycleManager().evaluate(
        state(quantity=100, initial=100, stop="95"), mark_price=D("106"), now=NOW, policy=policy
    )
    assert decision.action is LifecycleAction.ADD and decision.quantity == 20
    assert decision.proposed_stop_price == D("100")
    capped = PositionLifecycleManager().evaluate(
        state(quantity=110, initial=100, stop="95"), mark_price=D("106"), now=NOW, policy=policy
    )
    assert capped.action is LifecycleAction.HOLD
    loser = PositionLifecycleManager().evaluate(
        state(quantity=100, initial=100, stop="95"), mark_price=D("99"), now=NOW, policy=policy
    )
    assert loser.action is LifecycleAction.HOLD


def test_short_trailing_stop_and_take_profit_are_directionally_symmetric():
    policy = PositionLifecyclePolicy(
        break_even_activation_gain=D("0.03"), trailing_activation_gain=D("0.05"),
        trailing_distance_fraction=D("0.02"),
    )
    short = state(direction=PositionDirection.SHORT, extreme="95", stop="105", target="85")
    decision = PositionLifecycleManager().evaluate(
        short, mark_price=D("90"), now=NOW + timedelta(hours=1), policy=policy
    )
    assert decision.action is LifecycleAction.HOLD
    assert decision.proposed_stop_price == D("91.80")
    target = PositionLifecycleManager().evaluate(
        short, mark_price=D("84"), now=NOW + timedelta(hours=1), policy=policy
    )
    assert target.action is LifecycleAction.EXIT_ALL and target.reason == "take_profit_hit"
