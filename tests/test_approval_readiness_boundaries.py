"""Behavior tests for malformed approvals, scope binding and readiness reports.

Synthetic paper brokers only; the LIVE route must remain unavailable.
"""
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from quant_ai.brokers.base import ExecutionResult
from quant_ai.brokers.router import BrokerRouter
from quant_ai.compliance.gates import ComplianceContext, ExecutionMode, execution_allowed
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    InstrumentBoundOrderIntent,
    Market,
    OrderIntent,
    Side,
)
from quant_ai.execution.live_factory import LiveMoneyDisabledError
from quant_ai.operations.readiness import ReadinessCheck, ReadinessReport


def context(mode=ExecutionMode.PAPER):
    return ComplianceContext(Market.USA, AssetClass.EQUITY, mode)


def order(**changes):
    value = OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "synthetic")
    return replace(value, **changes)


@pytest.mark.parametrize("field", ["live_approved", "jurisdiction_approved"])
@pytest.mark.parametrize("bad", ["false", "true", 1, 0, None, [], [True]])
def test_approvals_must_be_actual_booleans(field, bad):
    ctx = replace(context(ExecutionMode.LIVE), live_approved=True, jurisdiction_approved=True)
    assert execution_allowed(replace(ctx, **{field: bad})) is False


@pytest.mark.parametrize("bad", [None, "PAPER", "LIVE", "UNKNOWN", 1, True, SimpleNamespace(value="PAPER")])
def test_unknown_or_untyped_modes_refuse(bad):
    ctx = replace(context(), execution_mode=bad, live_approved=True, jurisdiction_approved=True)
    assert execution_allowed(ctx) is False


@pytest.mark.parametrize("field,bad", [("market", "USA"), ("market", None),
    ("asset_class", "EQUITY"), ("asset_class", None)])
def test_untyped_scope_refuses(field, bad):
    assert execution_allowed(replace(context(), **{field: bad})) is False


@pytest.mark.parametrize("bad", [None, {}, {"execution_mode": "PAPER"},
    SimpleNamespace(execution_mode=ExecutionMode.PAPER, live_approved=True, jurisdiction_approved=True)])
def test_untyped_context_refuses_without_attribute_errors(bad):
    assert execution_allowed(bad) is False


@pytest.mark.parametrize("mode", [ExecutionMode.RESEARCH, ExecutionMode.PAPER])
def test_existing_nonlive_modes_still_dispatch_to_paper(mode):
    paper, live = Mock(), Mock()
    paper.submit.return_value = ExecutionResult("synthetic", "FILLED", 1, Decimal(100))
    request = order()
    assert BrokerRouter(paper, live).submit(request, context(mode)).status == "FILLED"
    paper.submit.assert_called_once_with(request)
    live.submit.assert_not_called()


@pytest.mark.parametrize("changes", [{"market": Market.INDIA}, {"asset_class": AssetClass.ETF},
    {"market": "USA"}, {"asset_class": "EQUITY"}])
def test_approval_scope_must_match_actual_order_before_broker_call(changes):
    paper, live = Mock(), Mock()
    with pytest.raises(PermissionError):
        BrokerRouter(paper, live).submit(order(**changes), context())
    paper.submit.assert_not_called()
    live.submit.assert_not_called()


def test_untyped_order_refuses_before_broker_call():
    paper, live = Mock(), Mock()
    with pytest.raises(PermissionError):
        BrokerRouter(paper, live).submit(SimpleNamespace(market=Market.USA, asset_class=AssetClass.EQUITY), context())
    paper.submit.assert_not_called()
    live.submit.assert_not_called()


def test_valid_bound_order_keeps_existing_paper_route():
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    request = InstrumentBoundOrderIntent("AAPL", Market.USA, Side.BUY, 1,
        Decimal(100), "synthetic", instrument=instrument)
    paper = Mock()
    BrokerRouter(paper).submit(request, context())
    paper.submit.assert_called_once_with(request)


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_both_sides_remain_live_disabled_even_with_true_approvals(side):
    paper, live = Mock(), Mock()
    ctx = replace(context(ExecutionMode.LIVE), live_approved=True, jurisdiction_approved=True)
    assert execution_allowed(ctx) is True
    with pytest.raises(LiveMoneyDisabledError):
        BrokerRouter(paper, live).submit(order(side=side), ctx)
    paper.submit.assert_not_called()
    live.submit.assert_not_called()


@pytest.mark.parametrize("bad", ["false", "passed", "true", 1, 0, None, [], [True]])
def test_readiness_does_not_accept_truthy_status_coercion(bad):
    with pytest.raises(ValueError):
        ReadinessCheck("broker", bad)


@pytest.mark.parametrize("name", ["", "  ", "broker ", " broker", None, 1])
def test_readiness_names_are_nonempty_canonical_strings(name):
    with pytest.raises(ValueError):
        ReadinessCheck(name, True)


def test_duplicate_readiness_names_refuse_instead_of_overwriting_meaning():
    with pytest.raises(ValueError):
        ReadinessReport((ReadinessCheck("broker", True), ReadinessCheck("broker", False)))


@pytest.mark.parametrize("checks", [None, "passed", {"broker": True}, (True,),
    (SimpleNamespace(name="broker", ready=True),)])
def test_untyped_report_inventory_refuses(checks):
    with pytest.raises(ValueError):
        ReadinessReport(checks)


def test_caller_list_cannot_change_a_frozen_report():
    checks = [ReadinessCheck("broker", True)]
    report = ReadinessReport(checks)
    checks.append(ReadinessCheck("risk", False))
    assert isinstance(report.checks, tuple)
    assert report.ready is True and len(report.checks) == 1


def test_empty_and_explicitly_blocked_reports_remain_not_ready():
    assert ReadinessReport(()).ready is False
    report = ReadinessReport((ReadinessCheck("feed", True), ReadinessCheck("broker", False)))
    assert report.ready is False
    assert report.blockers == (ReadinessCheck("broker", False),)


@pytest.mark.parametrize("live,jurisdiction", [(False, False), (False, True), (True, False)])
def test_typed_false_approvals_never_pass_live_compliance(live, jurisdiction):
    assert execution_allowed(replace(context(ExecutionMode.LIVE),
        live_approved=live, jurisdiction_approved=jurisdiction)) is False


def test_malformed_approval_also_refuses_paper_dispatch():
    paper = Mock()
    with pytest.raises(PermissionError):
        BrokerRouter(paper).submit(order(), replace(context(), live_approved="false"))
    paper.submit.assert_not_called()


def test_bad_scope_does_not_consume_an_actual_paper_order_id():
    from quant_ai.execution.paper_broker import PaperBroker
    paper = PaperBroker()
    router = BrokerRouter(paper)
    with pytest.raises(PermissionError):
        router.submit(order(market=Market.INDIA), context())
    fill = router.submit(order(), context())
    assert fill.order_id == "PAPER-00000001" and fill.filled_quantity == 1


def test_approval_values_are_not_coerced_through_user_code():
    class DangerousTruthiness:
        def __bool__(self):
            raise AssertionError("must not call arbitrary approval truthiness")
    assert execution_allowed(replace(context(), live_approved=DangerousTruthiness())) is False


@pytest.mark.parametrize("detail", [None, True, {}, ["message"]])
def test_invalid_readiness_detail_refuses(detail):
    with pytest.raises(ValueError):
        ReadinessCheck("broker", True, detail)
