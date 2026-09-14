from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.broker_observation import capture_kite, inspect_capture
from quant_ai.execution.broker_reads import BrokerReadError, ib_funds, kite_positions
from quant_ai.execution.live_brokers import (
    InteractiveBrokersAdapter,
    ReadOnlyJsonTransport,
    ZerodhaKiteAdapter,
)
from quant_ai.execution.paper_ledger import PaperBrokerService

NOW = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)


def envelope(data):
    return {"status": "success", "data": data}


def order(**changes):
    return {"order_id":"order-1", "instrument_token":738561, "tradingsymbol":"RELIANCE", "exchange":"NSE", "product":"CNC",
        "transaction_type":"BUY", "quantity":3, "filled_quantity":3, "pending_quantity":0, "cancelled_quantity":0,
        "average_price":100, "status":"COMPLETE", "variety":"regular", "order_timestamp":"2026-09-11 11:00:00", "exchange_order_id":"ex-1", **changes}


def trade(**changes):
    return {"trade_id":"trade-1", "order_id":"order-1", "instrument_token":738561, "tradingsymbol":"RELIANCE", "exchange":"NSE", "product":"CNC",
        "transaction_type":"BUY", "quantity":1, "average_price":98, "fill_timestamp":"2026-09-11 11:01:00", "exchange_order_id":"ex-1", **changes}


class Transport:
    def __init__(self, orders=None, trades=None):
        self.orders = [order()] if orders is None else orders
        self.trades = [trade(), trade(trade_id="trade-2", quantity=2, average_price=101)] if trades is None else trades
        self.calls = []
        self.change = None
    def get(self, path, params=None):
        self.calls.append(path)
        result = {"/user/profile": envelope({"user_id":"AB1234", "email":"PRIVATE_SENTINEL"}),
            "/orders":envelope(self.orders), "/trades":envelope(self.trades)}[path]
        if self.change:
            return self.change(path, deepcopy(result), self.calls)
        return deepcopy(result)


def capture(transport=None, **kwargs):
    return capture_kite(transport or Transport(), "AB1234", "india-paper", clock=lambda: NOW, **kwargs)


def test_split_execution_fills_reconcile_without_paper_or_profile_data():
    transport = Transport()
    report = capture(transport)
    assert inspect_capture(report) == {"status":"consistent", "issueCount":0, "issues":[], "orderCount":1,"tradeCount":2,"openOrderCount":0}
    assert report["trades"][0]["at"] == "2026-09-11T05:31:00+00:00"
    assert "AB1234" not in str(report) and "PRIVATE_SENTINEL" not in str(report)
    assert transport.calls == ["/user/profile", "/orders", "/trades", "/orders", "/trades", "/user/profile"]


@pytest.mark.parametrize("changes,code", [
    ({"filled_quantity":2}, "filled_quantity_mismatch"), ({"quantity":2}, "quantity_components_exceed_order"),
    ({"average_price":101}, "complete_average_price_mismatch"), ({"status":"MYSTERY"}, "unsupported_status"),
    ({"variety":"iceberg"}, "unsupported_order_variety"), ({"product":"MIS"}, "trade_identity_mismatch"),
    ({"status":"CANCELLED", "pending_quantity":1}, "terminal_order_still_pending"),
    ({"status":"REJECTED"}, "rejected_order_has_fills"), ({"order_timestamp":"2026-09-11 12:01:00"}, "order_time_after_capture"),
])
def test_inconsistent_quantities_identity_status_and_price_are_visible(changes, code):
    result = inspect_capture(capture(Transport([order(**changes)])))
    assert result["status"] == "issues"
    assert code in [i["code"] for i in result["issues"]]


def test_cancelled_partial_order_retains_executions_without_average_price_assumption():
    transport = Transport([order(status="CANCELLED", filled_quantity=1, cancelled_quantity=2, average_price=0)], [trade()])
    result = inspect_capture(capture(transport))
    assert result["status"] == "consistent" and result["tradeCount"] == 1


def test_open_partial_fills_are_not_completion_or_empty_evidence():
    result = inspect_capture(capture(Transport([order(status="OPEN", filled_quantity=1, pending_quantity=2, average_price=0)], [trade()])))
    assert result["status"] == "consistent" and result["openOrderCount"] == 1
    assert inspect_capture(capture(Transport([], [])))["status"] == "empty"
    assert inspect_capture(capture(Transport([], [trade()]))) ["status"] == "issues"


@pytest.mark.parametrize("field,value", [("quantity",1.5),("quantity",True),("quantity",2**53),("average_price","NaN"),("average_price",-1),("order_timestamp","2026-09-11T11:00:00")])
def test_malformed_evidence_rejected(field, value):
    with pytest.raises(BrokerReadError):
        capture(Transport([order(**{field:value})]))


def test_capture_change_and_account_switch_cannot_be_reported_consistent():
    t = Transport()
    t.change = lambda path, result, calls: envelope([order(filled_quantity=2)]) if path == "/orders" and calls.count(path) == 2 else result
    assert inspect_capture(capture(t))["status"] == "changing"
    t = Transport()
    t.change = lambda path, result, calls: envelope({"user_id":"WRONG"}) if path == "/user/profile" and calls.count(path) == 2 else result
    with pytest.raises(BrokerReadError, match="selected external account"):
        capture(t)


@pytest.mark.parametrize("end", [NOW+timedelta(seconds=31), NOW-timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_unbounded_capture_interval_is_rejected(end):
    times = iter([NOW,end])
    with pytest.raises(BrokerReadError):
        capture_kite(Transport(), "AB1234", "india-paper", clock=lambda: next(times))


def test_duplicate_and_failed_envelopes_are_not_silent_empty_accounts():
    for t in [Transport([order(), order()]), Transport(trades=[trade(), trade()])]:
        with pytest.raises(BrokerReadError, match="Duplicate"):
            capture(t)
    t = Transport()
    t.change = lambda path, result, calls: {"status":"error","data":[]} if path == "/orders" else result
    with pytest.raises(BrokerReadError, match="successful"):
        capture(t)


@pytest.mark.parametrize("adapter_name", ["kite", "ib"])
def test_all_execution_facing_account_reads_stay_on_paper_ledger(tmp_path, adapter_name):
    paper = PaperBrokerService(tmp_path / "paper.db", starting_capital=Decimal(10000))
    transport = Transport()
    adapter = ZerodhaKiteAdapter("key","token",paper, transport=transport) if adapter_name == "kite" else InteractiveBrokersAdapter("DU123",paper,transport=transport)
    adapter.submit_order(OrderIntent("RELIANCE",Market.INDIA,Side.BUY,2,Decimal(100),"test",tenant_id="isolated"))
    assert adapter.get_positions("isolated") == paper.get_positions("isolated")
    assert adapter.get_margin("isolated") == paper.get_margin("isolated")
    assert adapter.get_account_summary("isolated") == paper.get_account_summary("isolated")
    assert transport.calls == []


def test_ib_full_pagination_fractional_contracts_and_failure_boundaries(tmp_path):
    paper = PaperBrokerService(tmp_path / "paper.db")
    class Pages:
        def __init__(self):
            self.calls = []
        def get(self, path):
            self.calls.append(path)
            if path == "/portfolio/accounts":
                return [{"id":"DU123"}]
            page = int(path.split("/")[-1])
            return [] if page == 2 else [{"acctId":"DU123","conid":page+1,"position":"0.125","avgCost":"500", "contractDesc":"XYZ", "assetClass":"UNKNOWN"}]
    t = Pages()
    adapter = InteractiveBrokersAdapter("DU123",paper,transport=t)
    rows = adapter.read_external_positions()
    assert len(rows) == 2 and rows[1].quantity == Decimal("0.125")
    assert rows[0].security_type == "UNKNOWN" and rows[0].currency is None
    assert t.calls[-1].endswith("/2")
    t.get = lambda path: [{"id":"OTHER"}]
    with pytest.raises(BrokerReadError,match="Selected IBKR"):
        adapter.read_external_positions()


def test_summary_missing_values_stay_unknown_and_mixed_currency_fails():
    assert ib_funds({"accountcode":{"value":"DU123"}},"DU123").cash_balance is None
    with pytest.raises(BrokerReadError,match="mixes currencies"):
        ib_funds({"accountcode":{"value":"DU123"}, "totalcashvalue":{"amount":1,"currency":"USD"},"netliquidation":{"amount":2,"currency":"EUR"}},"DU123")
    with pytest.raises(BrokerReadError):
        kite_positions(envelope({"net":[{"instrument_token":1,"tradingsymbol":"OPT","exchange":"NFO","product":"NRML","quantity":"0.5","average_price":1}]}),"AB1234")


@pytest.mark.parametrize("url", ["http://localhost:5000", "https://user:pass@broker.example", "https://broker.example/?secret=x"])
def test_transport_requires_verified_https_origin(url):
    with pytest.raises(BrokerReadError):
        ReadOnlyJsonTransport(url)


def test_transport_rejects_redirects_large_duplicate_and_nonfinite_payloads(monkeypatch):
    from urllib.request import Request

    from quant_ai.execution import live_brokers
    with pytest.raises(BrokerReadError,match="redirected"):
        live_brokers._NoRedirect().redirect_request(Request("https://broker.example", headers={"Authorization":"SYNTHETIC"}),None,302,"",{},"https://other.example")
    class Response:
        def __init__(self,body):
            self.body=body
        def __enter__(self):
            return self
        def __exit__(self,*_):
            pass
        def read(self,size):
            return self.body[:size]
    class Opener:
        def __init__(self,body):
            self.body=body
        def open(self,request,timeout):
            assert request.get_method()=="GET"
            return Response(self.body)
    for body in [b"x"*8_000_001, b'{"data":1,"data":2}', b'{"data":NaN}']:
        monkeypatch.setattr(live_brokers,"build_opener",lambda handler, body=body:Opener(body))
        with pytest.raises(BrokerReadError):
            ReadOnlyJsonTransport("https://broker.example").get("/orders")
    monkeypatch.setattr(live_brokers,"build_opener",lambda handler:Opener(b'{"price":0.1234567890123456789}'))
    assert ReadOnlyJsonTransport("https://broker.example").get("/quote")["price"]==Decimal("0.1234567890123456789")
    for path in ["//evil.example", "/../orders", "/orders?secret=x", "/orders#x"]:
        with pytest.raises(BrokerReadError):
            ReadOnlyJsonTransport("https://broker.example").get(path)


def test_ib_wrong_account_duplicate_page_and_page_limit_never_return_partial_portfolio(tmp_path):
    paper=PaperBrokerService(tmp_path/"paper.db")
    class BadPages:
        mode="account"
        def get(self,path):
            if path=="/portfolio/accounts":
                return [{"id":"DU123"}]
            page=int(path.split("/")[-1])
            return [{"acctId":"OTHER" if self.mode=="account" else "DU123", "conid":page+1 if self.mode=="limit" else 1,"position":"0.5","avgCost":100,"ticker":"ABC"}]
    t=BadPages();adapter=InteractiveBrokersAdapter("DU123",paper,transport=t)
    for mode,reason in [("account","account identity"),("duplicate","Duplicate"),("limit","100-page")]:
        t.mode=mode
        with pytest.raises(BrokerReadError,match=reason):
            adapter.read_external_positions()
