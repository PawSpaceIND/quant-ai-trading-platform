"""The protective state the engine knows, as the dashboard has to be able to read it.

Two payload blocks are under test. ``protectionSweep`` says whether each stored stop can
currently be acted on; ``riskGates`` says which opt-in entry controls the operator armed.
Both exist because an operator cannot otherwise tell a quiet engine from a blind one, and
every assertion here is about keeping those two apart.

Pin the daemon's existing injectable clock to the fixture's quote instant. Otherwise
quotes stamped at module collection become stale merely because earlier tests ran
for more than the freshness window. The actual resolver and freshness limits remain
unchanged, including the deliberately 200-second-old quote in the stale-data case.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_pilot_closure import publish_tick
from test_pilot_closure import runner_for as pilot_runner_for

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.governance.event_calendar import EventCalendar, ScheduledEvent

NOW = datetime.now(timezone.utc)


def runner_for(tmp_path):
    runner = pilot_runner_for(tmp_path)
    runner.daemon.clock = lambda: NOW
    return runner


def buy(runner):
    runner.daemon.tracker.broker.buy(
        OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(100), "test",
                    tenant_id="pilot", stop_price=Decimal(95), take_profit_price=Decimal(120))
    )


def runtime(runner):
    row = runner.daemon.tracker.broker._connection.execute(
        "SELECT payload FROM pilot_runtime"
    ).fetchone()
    return json.loads(row[0])


def gate(payload, gate_id):
    return next(item for item in payload["riskGates"]["gates"] if item["id"] == gate_id)


def test_a_clean_sweep_is_published_as_swept_and_not_as_silence(tmp_path):
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW)
    buy(runner)
    runner.daemon.protection_tick(NOW)
    sweep = runtime(runner)["protectionSweep"]
    assert sweep["schema"] == "pramana.protection_sweep.v1" and sweep["tenantId"] == "pilot"
    # The distinction the whole block exists for: an empty list after a sweep means the
    # engine looked and found nothing, and is published with the moment it looked.
    assert sweep["sweptAt"] == NOW.isoformat()
    assert sweep["unprotected"] == [] and sweep["rebased"] == []
    assert sweep["haltAfterSeconds"] == 120


def test_a_stop_that_cannot_be_priced_is_visible_before_the_halt_fires(tmp_path):
    runner = runner_for(tmp_path)
    # The only tick on the book is too old for the resolver to trust, while the grace
    # period still has minutes left: exactly the window in which nothing used to say
    # anything at all.
    publish_tick(runner, "100", NOW - timedelta(seconds=200))
    buy(runner)
    runner.daemon.protection_tick(NOW)
    sweep = runtime(runner)["protectionSweep"]
    assert not runner.daemon.kill_switch.engaged
    assert [row["symbol"] for row in sweep["unprotected"]] == ["INFY"]
    assert sweep["unprotected"][0]["unpricedSince"] == NOW.isoformat()
    assert sweep["rebased"] == []


def test_a_re_based_quote_names_the_symbol_whose_stop_is_suspended(tmp_path):
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW - timedelta(seconds=2))
    buy(runner)
    runner.daemon.protection_tick(NOW)
    # A step past the exchange band: the stored stop and cost basis no longer refer to the
    # same unit, so the stop is not evaluated and the position is unprotected in fact.
    publish_tick(runner, "20", NOW - timedelta(seconds=1))
    runner.daemon.protection_tick(NOW)
    sweep = runtime(runner)["protectionSweep"]
    assert sweep["rebased"] == ["INFY"]
    assert sweep["unprotected"] == []
    # A suspension is neither a liquidation nor a halt: the position is still held, and
    # the quote is 20 against a stop of 95.
    assert runner.daemon.tracker.broker.get_positions("pilot")


def test_unarmed_gates_report_as_unarmed_rather_than_as_nothing_to_report(tmp_path):
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW)
    runner.daemon.protection_tick(NOW)
    payload = runtime(runner)
    assert payload["riskGates"]["schema"] == "pramana.risk_gates.v1"
    assert {item["id"] for item in payload["riskGates"]["gates"]} == {
        "sector_concentration", "correlation_adjusted_gross", "book_expected_shortfall",
        "overnight_exposure", "overnight_gap_monitor", "event_blackout", "corporate_actions",
    }
    for item in payload["riskGates"]["gates"]:
        assert item["armed"] is False
        # The setting that arms it travels with the gate, so an operator reading a silent
        # control is told how to turn it on instead of assuming it already is on.
        assert item["setting"].startswith("PRAMANA_")
    assert gate(payload, "sector_concentration")["limit"] == 0.25
    assert gate(payload, "book_expected_shortfall")["limit"] == 0.03
    assert payload["protectionSweep"]["gapMonitor"] == {"armed": False, "unresolved": []}


def test_armed_gates_report_what_the_operator_actually_supplied(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GROSS_CAP", "0.25")
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GAP_MONITOR", "session")
    runner = runner_for(tmp_path)
    warden = runner.daemon.scheduler.pipeline.runtime.warden
    warden.book_risk.sector_map = {"INFY": "IT_SERVICES", "TCS": "IT_SERVICES"}
    warden.book_risk.history_provider = lambda symbols: {}
    today = NOW.astimezone(ZoneInfo("Asia/Kolkata")).date()
    runner.daemon.event_calendar = EventCalendar(
        (ScheduledEvent(today, today, "earnings", "INFY"),)
    )
    publish_tick(runner, "100", NOW)
    buy(runner)
    runner.daemon.protection_tick(NOW)
    payload = runtime(runner)
    assert gate(payload, "sector_concentration")["armed"] is True
    # Two mapped symbols in one group, not "a sector map exists": armed with an empty
    # file is a third state again and must not read like armed with a real one.
    assert gate(payload, "sector_concentration")["records"] == 2
    assert gate(payload, "sector_concentration")["groups"] == 1
    assert gate(payload, "correlation_adjusted_gross")["armed"] is True
    assert gate(payload, "overnight_exposure")["armed"] is True
    assert gate(payload, "overnight_gap_monitor")["armed"] is True
    assert payload["protectionSweep"]["gapMonitor"]["armed"] is True
    blackout = gate(payload, "event_blackout")
    assert blackout["armed"] is True and blackout["records"] == 1
    # A blackout in force right now, not only a refusal that already happened.
    assert blackout["blackouts"] == [{"category": "earnings", "symbol": "INFY"}]


def test_overnight_exposure_is_measured_against_its_cap_and_never_guessed(tmp_path):
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW)
    buy(runner)
    runner.daemon.protection_tick(NOW)
    overnight = gate(runtime(runner), "overnight_exposure")
    assert overnight["observedUnavailable"] is None
    assert 0 < overnight["observed"] < overnight["limit"]
    # Withheld with its cause when the account cannot support it. A zero here would read
    # as an empty book, which is the one thing an unusable account may not claim.
    runner.daemon.telemetry.publish_unavailable(NOW, "invalid_position_average")
    withheld = gate(runtime(runner), "overnight_exposure")
    assert withheld["observed"] is None
    assert withheld["observedUnavailable"] == "invalid_position_average"


def test_a_broken_event_calendar_cannot_halt_the_pilot_through_the_reporting_path(tmp_path):
    runner = runner_for(tmp_path)

    class Hostile:
        events = ()

        def local_day(self, moment):
            raise ValueError("operator file is unusable")

    runner.daemon.event_calendar = Hostile()
    publish_tick(runner, "100", NOW)
    buy(runner)
    runner.daemon.protection_tick(NOW)
    payload = runtime(runner)
    # ``EventCalendarError`` is a ``ValueError``, so an unguarded read here would reach the
    # publish guard, be recorded as an invalid valuation and halt the engine.
    assert payload["valuation"]["status"] == "available"
    assert gate(payload, "event_blackout")["blackouts"] == []
    assert not runner.daemon.kill_switch.engaged


@pytest.mark.parametrize("seconds", ["inf", "nan"])
def test_a_non_finite_halt_clock_is_published_as_absent_not_as_zero(tmp_path, monkeypatch, seconds):
    monkeypatch.setenv("PRAMANA_UNPROTECTED_HALT_SECONDS", seconds)
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW)
    runner.daemon.protection_tick(NOW)
    assert runtime(runner)["protectionSweep"]["haltAfterSeconds"] is None


def test_a_book_that_has_never_been_swept_is_not_published_as_clean(tmp_path):
    """The other half of the distinction, and the one that fails silently.

    Before the first sweep ``unprotected`` and ``rebased`` are empty for the same reason
    they are empty after a clean one, so the lists alone cannot separate "the engine
    looked and found nothing" from "the engine has not looked". Only a null ``sweptAt``
    carries that, which makes filling it in with the publish time - the obvious tidy-up -
    the exact change that would report silence as safety. Pinned so it cannot be made.
    """
    runner = runner_for(tmp_path)
    publish_tick(runner, "100", NOW)
    buy(runner)
    runner.daemon.telemetry.publish(NOW)  # publish without a protection sweep

    sweep = runtime(runner)["protectionSweep"]
    assert sweep["sweptAt"] is None, "an unswept engine must not claim a sweep"
    assert sweep["unprotected"] == [] and sweep["rebased"] == []
    assert sweep["checkedAt"] == NOW.isoformat(), "the publish still happened"


@pytest.mark.parametrize("delay", [timedelta(minutes=5), timedelta(days=1)])
def test_fixture_quotes_keep_their_declared_age_after_delayed_collection(tmp_path, monkeypatch, delay):
    monkeypatch.setattr(f"{__name__}.NOW", datetime.now(timezone.utc) - delay)
    for name in ("fresh", "stale", "rebased"):
        (tmp_path / name).mkdir()
    # The same production freshness checks must still distinguish these three cases.
    test_a_clean_sweep_is_published_as_swept_and_not_as_silence(tmp_path / "fresh")
    test_a_stop_that_cannot_be_priced_is_visible_before_the_halt_fires(tmp_path / "stale")
    test_a_re_based_quote_names_the_symbol_whose_stop_is_suspended(tmp_path / "rebased")
