"""Synthetic provider replies only. No provider authentication, real data or orders."""
from __future__ import annotations

import gzip
import importlib.util
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.resilience import HttpResponse, ProviderHttpError, ResilientHttpClient
from quant_ai.marketdata import kite_history as kh
from quant_ai.marketdata.history_selection import daily_history_from_env

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 17, 5, tzinfo=timezone.utc)
START = NOW - timedelta(days=200)
INSTRUMENT = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
HEADER = "instrument_token,tradingsymbol,exchange,segment,instrument_type,expiry,lot_size,tick_size\n"
MASTER = (HEADER + "101,INFY,NSE,NSE,EQ,,1,0.05\n202,TCS,NSE,NSE,EQ,,1,0.05\n").encode()
ROW = ["2026-09-16T09:15:00+0530", 100, 105, 95, 102, 1000]


def encoded(rows):
    return json.dumps({"status": "success", "data": {"candles": rows}}).encode()


class Client:
    def __init__(self, master=MASTER, candles=None):
        self.master = master
        self.candles = encoded([ROW]) if candles is None else candles
        self.calls = []
        self.failure = None

    def get_response(self, url, *, params=None, headers=None):
        self.calls.append((url, params))
        assert headers["X-Kite-Version"] == "3"
        if self.failure is not None:
            raise self.failure
        return HttpResponse(200, self.master if url.endswith("/NSE") else self.candles, {})


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    import socket
    def refused(*_args, **_kwargs):
        pytest.fail("External network request attempted")
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)
    for key in ("ZERODHA_API_KEY", "ZERODHA_ACCESS_TOKEN", "PRAMANA_DAILY_HISTORY_PROVIDER", "TRADING_LIVE_MONEY_ACTIVE"):
        monkeypatch.delenv(key, raising=False)


def feed(client=None):
    return kh.KiteDailyFeed("fixture", "dummy", client=client or Client())


def read(client):
    return feed(client).fetch_ohlcv(INSTRUMENT, START, NOW, "1d")


def test_opt_in_selection_is_lazy_and_reuses_real_provider():
    provider = daily_history_from_env(source="kite", environ={"ZERODHA_API_KEY": "fixture", "ZERODHA_ACCESS_TOKEN": "dummy"})
    assert isinstance(provider, kh.KiteDailyHistoryProvider)
    assert provider.provider_id == "zerodha-kite-daily-history"
    assert provider.cached(INSTRUMENT, NOW) == ()
    assert provider.feed.client.max_attempts == 1
    assert provider.feed.client.rate_limiter.rate == 1
    assert provider.feed.client.max_payload_bytes == kh.MAX_BYTES


@pytest.mark.parametrize("environment", [{}, {"ZERODHA_API_KEY": "fixture"}, {"ZERODHA_ACCESS_TOKEN": "dummy"}])
def test_missing_credentials_cannot_fall_back(environment):
    with pytest.raises(kh.KiteHistoryError, match="credentials_required"):
        daily_history_from_env(source="kite", environ=environment)


@pytest.mark.parametrize("value", ["true", "yes", "", "FALSEE"])
def test_kite_selection_cannot_acquire_live_authority(value):
    with pytest.raises(kh.KiteHistoryError, match="paper_only"):
        daily_history_from_env(source="kite", environ={"TRADING_LIVE_MONEY_ACTIVE": value})


def test_default_yahoo_and_none_remain_unchanged():
    factory_calls = []
    expected = object()
    def yahoo(client):
        factory_calls.append(client)
        return expected
    assert daily_history_from_env(environ={}, yahoo_factory=yahoo) is expected
    assert len(factory_calls) == 1
    assert daily_history_from_env(source="none", environ={}) is None
    with pytest.raises(kh.KiteHistoryError, match="unsupported"):
        daily_history_from_env(source="not-a-source", environ={})


def test_minute_warmup_uses_read_only_kite_history_and_returns_closed_bars():
    rows = []
    start = datetime(2026, 9, 16, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    for index in range(60):
        opened = start + timedelta(minutes=index)
        rows.append([
            opened.isoformat(), 100, 101, 99, 100 + index / 100, 1000 + index,
        ])
    client = Client(candles=encoded(rows))
    provider = kh.KiteMinuteWarmupProvider("fixture", "dummy", client=client)

    result = provider.fetch(INSTRUMENT, NOW)

    assert len(result) == 60
    assert result[0].timestamp == (start + timedelta(minutes=1)).astimezone(timezone.utc)
    assert result[-1].timestamp == (start + timedelta(minutes=60)).astimezone(timezone.utc)
    assert client.calls[0] == (kh.BASE + "/instruments/NSE", None)
    assert client.calls[1][0] == kh.BASE + "/instruments/historical/101/minute"
    assert client.calls[1][1]["continuous"] == client.calls[1][1]["oi"] == "0"


def test_daily_candles_derive_tokens_and_preserve_decimal_and_source():
    client = Client(candles=b'{"status":"success","data":{"candles":[["2026-09-16T09:15:00+0530",100,105,95,100.123456789012345678901,1000]]}}')
    reader = feed(client)
    result = reader.fetch_ohlcv(INSTRUMENT, START, NOW, "1d")
    assert result[0].close == Decimal("100.123456789012345678901")
    assert result[0].timestamp.utcoffset() is not None
    assert result[0].instrument == INSTRUMENT
    assert client.calls[0] == (kh.BASE + "/instruments/NSE", None)
    assert client.calls[1][0] == kh.BASE + "/instruments/historical/101/day"
    assert client.calls[1][1]["continuous"] == client.calls[1][1]["oi"] == "0"
    assert client.calls[1][1]["to"] == "2026-09-17 10:30:00"
    evidence = reader.evidence()["INFY"]
    assert evidence["closedRows"] == 1 and evidence["instrumentToken"] == "101"
    assert len(evidence["masterSha256"]) == len(evidence["responseSha256"]) == 64
    assert "dummy" not in json.dumps(evidence)
    evidence["closedRows"] = 55
    assert reader.evidence()["INFY"]["closedRows"] == 1


@pytest.mark.parametrize("change", [
    {"market": Market.USA}, {"exchange": "BSE"}, {"currency": "USD"},
    {"tradable": False}, {"symbol": "../orders"}, {"asset_class": AssetClass.BOND},
])
def test_out_of_scope_instruments_refuse_before_requests(change):
    client = Client()
    with pytest.raises(kh.KiteHistoryError, match="nse_cash_only"):
        feed(client).fetch_ohlcv(replace(INSTRUMENT, **change), START, NOW, "1d")
    assert client.calls == []


@pytest.mark.parametrize("problem", ["duplicate_symbol", "duplicate_token", "bad_token", "overflow", "expiry", "wrong_exchange", "missing_header", "duplicate_header", "bad_lot", "nan_tick"])
def test_bad_master_never_reaches_candle_request(problem):
    master = MASTER.decode()
    if problem == "duplicate_symbol": master += "303,INFY,NSE,NSE,EQ,,1,0.05\n"
    elif problem == "duplicate_token": master = master.replace("202,TCS", "101,TCS")
    elif problem == "bad_token": master = master.replace("101,INFY", "../,INFY")
    elif problem == "overflow": master = master.replace("101,INFY", "4294967296,INFY")
    elif problem == "expiry": master = master.replace("EQ,,1", "EQ,2026-09-01,1")
    elif problem == "wrong_exchange": master = master.replace(",NSE,NSE,", ",BSE,NSE,")
    elif problem == "missing_header": master = master.replace("instrument_token", "other")
    elif problem == "duplicate_header": master = master.replace("tick_size", "lot_size")
    elif problem == "bad_lot": master = master.replace(",1,0.05", ",0,0.05")
    else: master = master.replace("0.05", "NaN")
    client = Client(master=master.encode())
    with pytest.raises(kh.KiteHistoryError): read(client)
    assert len(client.calls) == 1


def test_missing_symbol_and_wrong_units_refuse_without_history():
    for instrument in (replace(INSTRUMENT, symbol="ABSENT"), replace(INSTRUMENT, lot_size=10), replace(INSTRUMENT, tick_size=Decimal(1))):
        client = Client()
        with pytest.raises(kh.KiteHistoryError):
            feed(client).fetch_ohlcv(instrument, START, NOW, "1d")
        assert len(client.calls) == 1


def test_gzip_master_and_daily_master_reuse():
    client = Client(master=gzip.compress(MASTER))
    reader = feed(client)
    reader.fetch_ohlcv(INSTRUMENT, START, NOW, "1d")
    reader.fetch_ohlcv(replace(INSTRUMENT, symbol="TCS"), START, NOW, "1d")
    assert len([url for url, _ in client.calls if url.endswith("/NSE")]) == 1
    reader.fetch_ohlcv(INSTRUMENT, START, NOW + timedelta(days=1), "1d")
    assert len([url for url, _ in client.calls if url.endswith("/NSE")]) == 2


@pytest.mark.parametrize("change", ["naive", "future", "outside", "bool_price", "text_price", "negative", "range", "negative_volume", "fractional_volume", "missing", "extra"])
def test_invalid_candles_refuse_whole_series(change):
    row = ROW.copy()
    if change == "naive": row[0] = "2026-09-16T09:15:00"
    elif change == "future": row[0] = "2026-09-18T09:15:00+0530"
    elif change == "outside": row[0] = "2020-09-16T09:15:00+0530"
    elif change == "bool_price": row[1] = True
    elif change == "text_price": row[1] = "100"
    elif change == "negative": row[1] = -1
    elif change == "range": row[4] = 999
    elif change == "negative_volume": row[-1] = -1
    elif change == "fractional_volume": row[-1] = 1.5
    elif change == "missing": row.pop()
    else: row.append(1)
    with pytest.raises(kh.KiteHistoryError): read(Client(candles=encoded([row])))


@pytest.mark.parametrize("body", [b'null', b'{"status":"error","message":"PRIVATE"}', b'{"status":"success","data":{}}', b'{"status":"success","data":{"candles":null}}', b'{"status":"success","status":"success","data":{"candles":[]}}', encoded([ROW, ROW]), encoded([ROW]).replace(b'100,', b'NaN,', 1)])
def test_invalid_envelope_or_duplicate_or_nonfinite_is_not_silently_accepted(body):
    with pytest.raises(kh.KiteHistoryError): read(Client(candles=body))


def test_live_session_is_not_counted_as_completed_history():
    today = ["2026-09-17T09:15:00+0530", *ROW[1:]]
    result = read(Client(candles=encoded([ROW, today])))
    assert len(result) == 1 and result[0].timestamp.date().isoformat() == "2026-09-16"
    assert read(Client(candles=encoded([]))) == ()


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_latched_and_message_never_disclosed(status):
    client = Client()
    client.failure = ProviderHttpError(status, "PRIVATE_PROVIDER_SECRET")
    reader = feed(client)
    for _ in range(2):
        with pytest.raises(kh.KiteHistoryError) as problem:
            reader.fetch_ohlcv(INSTRUMENT, START, NOW, "1d")
        assert "PRIVATE_PROVIDER_SECRET" not in str(problem.value)
    assert len(client.calls) == 1


def test_failures_are_redacted_and_daily_abstention_is_cached(caplog):
    client = Client()
    client.failure = OSError("PRIVATE_PROVIDER_SECRET")
    provider = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert provider.fetch(INSTRUMENT, NOW) == provider.fetch(INSTRUMENT, NOW) == ()
    assert provider.cached(INSTRUMENT, NOW) == ()
    assert len(client.calls) == 1
    assert "PRIVATE_PROVIDER_SECRET" not in caplog.text and "dummy" not in repr(provider)


def test_exact_identity_and_observation_time_bound_daily_cache():
    client = Client()
    provider = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert provider.cached(INSTRUMENT, NOW) == () and client.calls == []
    original = provider.fetch(INSTRUMENT, NOW)
    assert provider.fetch(INSTRUMENT, NOW) == original
    assert len(client.calls) == 2
    assert provider.cached(INSTRUMENT, NOW - timedelta(minutes=1)) == ()
    assert provider.fetch(INSTRUMENT, NOW - timedelta(minutes=1)) == ()
    assert provider.cached(INSTRUMENT, NOW + timedelta(days=1)) == ()
    etf = replace(INSTRUMENT, asset_class=AssetClass.ETF)
    assert provider.cached(etf, NOW) == ()
    assert provider.fetch(etf, NOW)[0].instrument == etf
    assert len(client.calls) == 3


def test_transport_refuses_orders_other_hosts_and_redirects(monkeypatch):
    reader = kh.KiteHistoryTransport()
    for method, url in [("POST", kh.BASE + "/orders/regular"), ("GET", "http://api.kite.trade/instruments/NSE"), ("GET", "https://evil.invalid/instruments/NSE"), ("GET", kh.BASE + "/orders"), ("GET", kh.BASE + "/instruments/NSE?x=1")]:
        with pytest.raises(kh.KiteHistoryError, match="endpoint_refused"):
            reader.request(method, url, params=None, headers={}, timeout_seconds=5, max_bytes=10)
    assert kh._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid") is None


def test_rate_limit_opens_existing_circuit_without_second_request():
    class Limited:
        calls = 0
        def request(self, *_args, **_kwargs):
            self.calls += 1
            return HttpResponse(429, b"", {})
    transport = Limited()
    client = ResilientHttpClient(transport, max_attempts=1)
    reader = feed(client)
    for name in ("INFY", "TCS"):
        with pytest.raises(kh.KiteHistoryError):
            reader.fetch_ohlcv(replace(INSTRUMENT, symbol=name), START, NOW, "1d")
    assert transport.calls == 1


def test_daemon_and_preflight_select_the_same_adapter(monkeypatch, tmp_path, capsys):
    from quant_ai import daemon
    monkeypatch.setenv("PRAMANA_DAILY_HISTORY_PROVIDER", "kite")
    monkeypatch.setenv("ZERODHA_API_KEY", "fixture")
    monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "dummy")
    assert isinstance(daemon._env_daily_history_provider(), kh.KiteDailyHistoryProvider)
    spec = importlib.util.spec_from_file_location("kite_preflight", ROOT / "scripts/check_pilot_risk_gates.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    observed = []
    def check(_d, _s, provider, _now):
        observed.append(type(provider))
        return {"dataReady": False, "hostAcceptance": False}
    monkeypatch.setattr(cli, "check", check)
    assert cli.main(["--online", "--history-provider", "kite"]) == 1
    assert observed == [kh.KiteDailyHistoryProvider]
    assert json.loads(capsys.readouterr().out)["historyEvidence"] == {}
    observed.clear()
    assert cli.main(["--history-provider", "kite"]) == 2
    assert observed == []


def test_protection_cache_read_does_not_wait_on_inflight_network():
    from threading import Event, Thread
    entered, release, observed = Event(), Event(), Event()
    class Slow(Client):
        def get_response(self, url, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().get_response(url, **kwargs)
    provider = kh.KiteDailyHistoryProvider("fixture", "dummy", client=Slow())
    fetching = Thread(target=provider.fetch, args=(INSTRUMENT, NOW))
    values = []
    def cached():
        values.append(provider.cached(INSTRUMENT, NOW))
        observed.set()
    reading = Thread(target=cached)
    fetching.start()
    try:
        assert entered.wait(2)
        reading.start()
        assert observed.wait(1), "Protection waited for an unrelated history HTTP request"
        assert values == [()]
    finally:
        release.set()
        fetching.join(3)
        if reading.ident is not None:
            reading.join(3)
    assert not fetching.is_alive() and not reading.is_alive()


@pytest.mark.parametrize("kind", ["raw_size", "decompressed_size", "row_count", "candle_count"])
def test_bounded_provider_payloads_are_not_silently_truncated(monkeypatch, kind):
    client = Client()
    if kind == "raw_size":
        monkeypatch.setattr(kh, "MAX_BYTES", 20)
    elif kind == "decompressed_size":
        client.master = gzip.compress(MASTER * 100)
        monkeypatch.setattr(kh, "MAX_BYTES", len(client.master) + 1)
    elif kind == "row_count":
        monkeypatch.setattr(kh, "MAX_ROWS", 1)
    else:
        monkeypatch.setattr(kh, "MAX_CANDLES", 0)
    with pytest.raises(kh.KiteHistoryError): read(client)


@pytest.mark.parametrize("kind", ["naive_start", "naive_end", "reversed", "too_long", "interval"])
def test_invalid_time_windows_refuse_before_provider(monkeypatch, kind):
    start, end, interval = START, NOW, "1d"
    if kind == "naive_start": start = start.replace(tzinfo=None)
    elif kind == "naive_end": end = end.replace(tzinfo=None)
    elif kind == "reversed": start = end
    elif kind == "too_long": start = end - timedelta(days=367)
    else: interval = "minute"
    client = Client()
    with pytest.raises(kh.KiteHistoryError):
        feed(client).fetch_ohlcv(INSTRUMENT, start, end, interval)
    assert client.calls == []


def test_incomplete_success_status_is_refused():
    class Redirect(Client):
        def get_response(self, *_args, **_kwargs):
            return HttpResponse(302, MASTER, {})
    with pytest.raises(kh.KiteHistoryError, match="response_refused"):
        read(Redirect())


def test_metadata_is_part_of_cache_identity():
    provider = kh.KiteDailyHistoryProvider("fixture", "dummy", client=Client())
    provider.fetch(INSTRUMENT, NOW)
    assert provider.cached(replace(INSTRUMENT, metadata={"basis": "other"}), NOW) == ()


def test_failed_new_day_does_not_return_yesterdays_evidence():
    client = Client()
    provider = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert provider.fetch(INSTRUMENT, NOW)
    assert provider.evidence()
    client.failure = OSError("PRIVATE")
    assert provider.fetch(INSTRUMENT, NOW + timedelta(days=1)) == ()
    assert provider.evidence() == {}


def test_runtime_fingerprint_changes_on_history_source(tmp_path):
    from test_pilot_required_risk_gates import daily, runner
    actual = runner(tmp_path, daily())
    actual.daemon.scheduler.pipeline.history = daily()  # Independent from the risk reader.
    actual.daemon.scheduler.pipeline.history.provider_id = "yahoo-daily-history"
    manifest = actual.daemon.strategy_manifest
    first = manifest.capture()
    actual.daemon.scheduler.pipeline.history.provider_id = "zerodha-kite-daily-history"
    second = manifest.capture()
    assert first["sha256"] != second["sha256"]


def test_rejected_auth_after_master_load_cannot_retry_on_another_symbol():
    class Auth(Client):
        def get_response(self, url, **kwargs):
            if not url.endswith("/NSE"):
                self.calls.append((url, kwargs.get("params")))
                raise ProviderHttpError(403, "PRIVATE")
            return super().get_response(url, **kwargs)
    client = Auth()
    reader = feed(client)
    for symbol in ("INFY", "TCS"):
        with pytest.raises(kh.KiteHistoryError):
            reader.fetch_ohlcv(replace(INSTRUMENT, symbol=symbol), START, NOW, "1d")
    assert len(client.calls) == 2


def test_nonfinite_unused_json_field_is_still_invalid_json():
    body = encoded([ROW]).replace(b'"status": "success"', b'"status": "success", "extra": NaN')
    with pytest.raises(kh.KiteHistoryError): read(Client(candles=body))


def test_out_of_order_daily_rows_do_not_get_silently_sorted():
    older = ["2026-09-15T09:15:00+0530", *ROW[1:]]
    with pytest.raises(kh.KiteHistoryError): read(Client(candles=encoded([ROW, older])))


def test_transport_read_is_bounded_and_does_not_follow_http_redirects(monkeypatch):
    events = []
    class Reply:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, count):
            events.append(count)
            return b"x" * count
    class Opener:
        def open(self, request, **kwargs):
            events.append(request.get_method())
            return Reply()
    def build(handler):
        assert isinstance(handler, kh._NoRedirect)
        return Opener()
    monkeypatch.setattr(kh.urllib.request, "build_opener", build)
    with pytest.raises(kh.KiteHistoryError, match="payload_too_large"):
        kh.KiteHistoryTransport().request("GET", kh.BASE + "/instruments/NSE", params=None,
                                         headers={}, timeout_seconds=5, max_bytes=10)
    assert events == ["GET", 11]


def test_read_only_entry_points_do_not_change_secret_files_or_defaults(monkeypatch):
    monkeypatch.setenv("ZERODHA_API_KEY", "fixture")
    monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "dummy")
    monkeypatch.setenv("PRAMANA_DAILY_HISTORY_PROVIDER", "kite")
    import os
    before = dict(os.environ)
    provider = daily_history_from_env()
    assert dict(os.environ) == before
    assert not hasattr(provider.feed, "place_order") and not hasattr(provider, "submit")
    assert "fixture" not in repr(provider) and "dummy" not in repr(provider)


def test_warden_configuration_identity_includes_risk_history_source(tmp_path):
    from test_pilot_required_risk_gates import daily, runner
    actual = runner(tmp_path, daily())
    warden = actual.daemon.scheduler.pipeline.runtime.warden
    before = warden.book_risk_inputs
    warden.book_risk.history_provider.provider.provider_id = "zerodha-kite-daily-history"
    after = warden.book_risk_inputs
    assert before["historySource"] != after["historySource"]
    assert after["historySource"] == "zerodha-kite-daily-history"
