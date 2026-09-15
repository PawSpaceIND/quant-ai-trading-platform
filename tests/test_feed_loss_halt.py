"""A stop that cannot be priced is not enforced, and the operator must be told.

Skipping an unknown mark keeps one sweep safe: a fabricated price could liquidate a
healthy position or bank a fictional target. But a dead feed makes every stop
unenforceable while the cadence still completes and the container still reports healthy.
These tests cover the halt that closes that window, and equally that it stays quiet on a
healthy feed, an empty book, or a brief gap.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from test_pilot_closure import publish_tick, runner_for

from quant_ai.domain.models import Market, OrderIntent, Side

SESSION = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)


def at(daemon, moment):
    """Pin the daemon clock; tick freshness and the sweep are judged against it."""
    daemon.clock = lambda: moment
    return moment


def feed(runner, price, moment):
    """Deliver a tick the engine will accept as fresh at ``moment``."""
    at(runner.daemon, moment)
    publish_tick(runner, price, moment)


def sweep(runner, moment):
    at(runner.daemon, moment)
    runner.daemon.protection_tick(moment)


def protected_position(runner, moment=SESSION):
    """One open INFY position carrying a 95 stop, priced by a fresh tick."""
    feed(runner, "100", moment)
    runner.daemon.tracker.broker.buy(
        OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(100), "test",
                    tenant_id=runner.daemon.tenant_id,
                    stop_price=Decimal(95), take_profit_price=Decimal(120))
    )


def test_a_dead_feed_halts_trading_once_the_grace_period_passes(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)
    grace = daemon.unprotected_halt_seconds

    # No tick is published from here on: the feed is gone.
    silent = SESSION + timedelta(minutes=10)
    sweep(runner, silent)
    assert daemon.exit_engine.unprotected == ("INFY",), "the sweep must report what it could not price"
    assert not daemon.kill_switch.engaged, "one unpriceable sweep is not yet a halt"

    sweep(runner, silent + timedelta(seconds=grace - 1))
    assert not daemon.kill_switch.engaged, "the grace period absorbs a brief gap"

    sweep(runner, silent + timedelta(seconds=grace))
    assert daemon.kill_switch.engaged
    assert "protection_unreachable" in (daemon.kill_switch.reason or "")
    assert "INFY" in (daemon.kill_switch.reason or "")


def test_the_halt_survives_a_restart(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)

    silent = SESSION + timedelta(minutes=10)
    sweep(runner, silent)
    sweep(runner, silent + timedelta(seconds=daemon.unprotected_halt_seconds))
    assert daemon.kill_switch.engaged

    # A new daemon on the same ledger: the halt must still be latched.
    restarted = runner_for(tmp_path).daemon
    assert restarted.kill_switch.engaged
    assert "protection_unreachable" in (restarted.kill_switch.reason or "")


def test_a_healthy_feed_never_halts(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)

    moment = SESSION
    for _ in range(10):
        moment += timedelta(seconds=daemon.unprotected_halt_seconds)
        feed(runner, "101", moment)          # comfortably above the 95 stop
        sweep(runner, moment)

    assert daemon.exit_engine.unprotected == ()
    assert not daemon.kill_switch.engaged


def test_a_recovered_feed_clears_the_timer(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)
    grace = daemon.unprotected_halt_seconds

    silent = SESSION + timedelta(minutes=10)
    sweep(runner, silent)                                  # unpriceable, timer starts

    recovered = silent + timedelta(seconds=grace - 5)
    feed(runner, "101", recovered)
    sweep(runner, recovered)                               # priced again, timer resets
    assert daemon.exit_engine.unprotected == ()

    # Past the original deadline, but the outage restarted from zero.
    sweep(runner, recovered + timedelta(seconds=grace - 1))
    assert not daemon.kill_switch.engaged


def test_an_empty_book_is_not_an_outage(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon

    moment = SESSION
    for _ in range(5):
        moment += timedelta(seconds=daemon.unprotected_halt_seconds)
        sweep(runner, moment)

    assert not daemon.kill_switch.engaged, "no position means nothing to protect"


def test_a_breach_still_fires_while_the_feed_is_alive(tmp_path):
    """The halt must not shadow the exit engine's actual job."""
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)

    breach = SESSION + timedelta(minutes=1)
    feed(runner, "90", breach)                             # through the 95 stop
    sweep(runner, breach)

    assert any(item.filled for item in daemon.protective_exits)
    assert daemon.exit_engine.unprotected == ()
    assert not daemon.tracker.broker.get_positions(daemon.tenant_id)
