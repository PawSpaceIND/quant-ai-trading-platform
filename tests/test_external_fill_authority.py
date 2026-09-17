"""No correctly hashed fill ID can substitute for matching source authority."""
from datetime import datetime, timedelta

import pytest
from test_broker_fill_namespace import create_order, scoped_capture

from quant_ai.execution.broker_lifecycle import _evidence_binding, _fill_source
from quant_ai.orders.execution_identity import external_fill_id
from quant_ai.orders.oms import DurableOms


@pytest.mark.parametrize("defect", ["account", "exchange", "day", "missing_binding", "missing_source"])
def test_external_fill_requires_matching_account_day_and_source(tmp_path, defect):
    capture = scoped_capture()
    trade = capture["trades"][0]
    at = datetime.fromisoformat(trade["at"])
    with DurableOms(tmp_path / "source.sqlite") as oms:
        row = create_order(oms, capture, "source-test")
        oms.submitted(row.client_order_id, broker_order_id=trade["orderId"], now=at)
        if defect != "missing_binding":
            oms.bind_broker_evidence(row.client_order_id,
                                    _evidence_binding(capture, capture["orders"][0]), now=at)
        source = _fill_source(trade, capture)
        if defect == "account":
            source["accountRef"] = "f" * 64
        elif defect == "exchange":
            source["exchange"] = "BSE"
        elif defect == "day":
            source["tradingDay"] = (at + timedelta(days=1)).date().isoformat()
        fill_id = external_fill_id(source)
        before = tuple(oms.db.iterdump())
        with pytest.raises(ValueError, match="external_fill_"):
            oms.fill(row.client_order_id, fill_id=fill_id, quantity=1, price=98,
                     broker_order_id=trade["orderId"], now=at,
                     source_identity=None if defect == "missing_source" else source)
        assert tuple(oms.db.iterdump()) == before


@pytest.mark.parametrize("defect", ["account", "day"])
def test_event_replay_rechecks_source_against_preceding_binding(tmp_path, monkeypatch, defect):
    from quant_ai.orders import oms as oms_module
    capture = scoped_capture()
    trade = capture["trades"][0]
    at = datetime.fromisoformat(trade["at"])
    with DurableOms(tmp_path / "replay.sqlite") as oms:
        row = create_order(oms, capture, "source-replay")
        oms.submitted(row.client_order_id, broker_order_id=trade["orderId"], now=at)
        oms.bind_broker_evidence(row.client_order_id,
                                _evidence_binding(capture, capture["orders"][0]), now=at)
        source = _fill_source(trade, capture)
        source["accountRef" if defect == "account" else "tradingDay"] = (
            "f" * 64 if defect == "account" else "2026-09-12"
        )
        # Simulate an old writer that failed to enforce source attribution. The resulting
        # event chain is correctly hashed; replay must still reject its wrong semantics.
        with monkeypatch.context() as patch:
            patch.setattr(oms_module, "assert_fill_source", lambda *args, **kwargs: None)
            oms.fill(row.client_order_id, fill_id=external_fill_id(source), quantity=1,
                     price=98, broker_order_id=trade["orderId"], now=at, source_identity=source)
        with pytest.raises(ValueError, match="external_fill_"):
            oms.verify(row.client_order_id)
