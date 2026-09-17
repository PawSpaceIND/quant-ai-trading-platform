"""Additional history checks use only synthetic replies and no external account."""
import json
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

import pytest
from test_kite_daily_history import INSTRUMENT, MASTER, NOW, Client, encoded, feed

from quant_ai.intelligence.resilience import ProviderHttpError
from quant_ai.marketdata import kite_history as kh


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def forbidden(*_a, **_k):
        pytest.fail("Unexpected external request")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)

@pytest.mark.parametrize("status", [401, 403])
def test_revoked_auth_invalidates_all_cached_symbols_and_evidence(status):
    client = Client()
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert source.fetch(INSTRUMENT, NOW)
    client.failure = ProviderHttpError(status, "PRIVATE_RESPONSE")
    assert source.fetch(replace(INSTRUMENT, symbol="TCS"), NOW) == ()
    calls = len(client.calls)
    assert source.cached(INSTRUMENT, NOW) == ()
    assert source.fetch(INSTRUMENT, NOW) == ()
    assert source.evidence() == {}
    assert len(client.calls) == calls


def test_window_ceiling_is_exact_not_rounded_down_to_whole_days():
    client = Client()
    with pytest.raises(kh.KiteHistoryError, match="window_invalid"):
        feed(client).fetch_ohlcv(INSTRUMENT, NOW - timedelta(days=366, seconds=1), NOW, "1d")
    assert client.calls == []


def test_unexpected_transport_diagnostic_is_sanitized(caplog):
    client = Client()
    client.failure = AttributeError("PRIVATE_RESPONSE")
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert source.fetch(INSTRUMENT, NOW) == ()
    assert "PRIVATE_RESPONSE" not in caplog.text


def test_empty_master_reuses_failed_daily_observation_without_second_call():
    client = Client(master=MASTER.splitlines()[0] + b"\n")
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert source.fetch(INSTRUMENT, NOW) == ()
    assert source.fetch(replace(INSTRUMENT, symbol="TCS"), NOW) == ()
    assert len(client.calls) == 1


def test_provider_evidence_read_does_not_wait_behind_network():
    entered, release, done = Event(), Event(), Event()
    class Slow(Client):
        def get_response(self, *args, **kwargs):
            entered.set()
            assert release.wait(3)
            return super().get_response(*args, **kwargs)
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=Slow())
    fetcher = Thread(target=source.fetch, args=(INSTRUMENT, NOW))
    reader = Thread(target=lambda: (source.evidence(), done.set()))
    fetcher.start()
    try:
        assert entered.wait(2)
        reader.start()
        assert done.wait(1), "Diagnostic evidence waited on network"
    finally:
        release.set()
        fetcher.join(3)
        if reader.ident is not None: reader.join(3)
    assert not fetcher.is_alive() and not reader.is_alive()


def test_closed_regular_bars_supply_enough_real_shape_without_filling_gaps():
    from test_pilot_required_risk_gates import DIRECTIVES

    from quant_ai.risk.book_history import DailyCloseHistory
    rows, day = [], NOW - timedelta(days=1)
    while len(rows) < 120:
        if day.weekday() < 5:
            rows.append([day.strftime("%Y-%m-%dT09:15:00+0530"), 100, 105, 95, 102, 1000])
        day -= timedelta(days=1)
    rows.reverse()
    lines = [MASTER.decode().splitlines()[0]]
    for index, instrument in enumerate(DIRECTIVES.watchlist, 1):
        lines.append(f"{index},{instrument.symbol},NSE,NSE,EQ,,1,0.05")
    client = Client(master=("\n".join(lines) + "\n").encode(), candles=encoded(rows))
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    history = DailyCloseHistory(source, DIRECTIVES.watchlist, clock=lambda: NOW, max_age=timedelta(days=7))
    symbols = tuple(item.symbol for item in DIRECTIVES.watchlist)
    observed = history(symbols)
    state = history.readiness(symbols, NOW)
    assert state["dataReady"] is True and state["records"] == 600
    assert state["alignedIntervals"] == 119 and state["coveredSymbols"] == 5
    assert all(len(observed[symbol]) == 120 for symbol in symbols)
    assert len(client.calls) == 6
    assert "PRIVATE" not in json.dumps(source.evidence())


@pytest.mark.parametrize("read_method", ["cached", "fetch"])
def test_direct_feed_rejection_revokes_previously_cached_history(read_method):
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=Client())
    assert source.fetch(INSTRUMENT, NOW)
    source.feed._reject_authentication()
    assert source.feed._evidence == {}
    assert getattr(source, read_method)(INSTRUMENT, NOW) == ()
    assert source._observations == {}


@pytest.mark.parametrize("status", [401, 403])
def test_raw_auth_response_is_also_latched(status):
    from quant_ai.intelligence.resilience import HttpResponse

    class RawRejected(Client):
        def get_response(self, url, **kwargs):
            self.calls.append((url, None))
            return HttpResponse(status, b"PRIVATE_RESPONSE", {})
    client = RawRejected()
    reader = feed(client)
    for _ in range(2):
        with pytest.raises(kh.KiteHistoryError):
            reader._get("/instruments/NSE")
    assert len(client.calls) == 1 and reader.authentication_rejected
    assert reader.evidence() == {}


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_decimal_nonfinite_guard_has_its_own_regression(value):
    from decimal import Decimal

    with pytest.raises(kh.KiteHistoryError, match="nonfinite"):
        kh._number(Decimal(value))


@pytest.mark.parametrize("master", [
    MASTER + b"303,INFY,NSE,NSE,EQ,,1,0.05,unexpected\n",
    MASTER.splitlines()[0] + b"\n101,INFY\n",
    MASTER.replace(b"101,INFY", b"101, infy "),
    MASTER.replace(b",EQ,,", b",FUT,,"),
])
def test_master_shape_and_canonical_identity_refuse_before_candles(master):
    client = Client(master=master)
    with pytest.raises(kh.KiteHistoryError):
        feed(client).fetch_ohlcv(INSTRUMENT, NOW-timedelta(days=200), NOW, "1d")
    assert len(client.calls) == 1


@pytest.mark.parametrize("key,token", [("", "dummy"), ("fixture", ""),
                                       ("bad\nkey", "dummy"), ("fixture", "bad:token")])
def test_header_unsafe_credentials_never_reach_a_request(key, token):
    client = Client()
    with pytest.raises(kh.KiteHistoryError, match="credentials_required"):
        kh.KiteDailyHistoryProvider(key, token, client=client)
    assert client.calls == []


def test_feed_evidence_fingerprints_match_actual_response_bytes():
    import hashlib

    client = Client()
    reader = feed(client)
    reader.fetch_ohlcv(INSTRUMENT, NOW-timedelta(days=200), NOW, "1d")
    record = reader.evidence()["INFY"]
    assert record["masterSha256"] == hashlib.sha256(client.master).hexdigest()
    assert record["responseSha256"] == hashlib.sha256(client.candles).hexdigest()
    assert record["priceBasis"] == "provider_supplied_adjustment_basis_not_independently_verified"


def test_auth_failure_during_refresh_discards_the_entire_observation_store():
    client = Client()
    source = kh.KiteDailyHistoryProvider("fixture", "dummy", client=client)
    assert source.fetch(INSTRUMENT, NOW)
    client.failure = ProviderHttpError(403, "PRIVATE_RESPONSE")
    assert source.fetch(INSTRUMENT, NOW + timedelta(days=1)) == ()
    assert source._observations == {} and source.feed._evidence == {}


@pytest.mark.parametrize("value", [True, "100"])
def test_numeric_parser_rejects_non_json_number_types(value):
    with pytest.raises(kh.KiteHistoryError, match="numeric_value_required"):
        kh._number(value)


@pytest.mark.parametrize("body,reason", [
    (MASTER.splitlines()[0] + b"\n", "master_empty"),
    (MASTER.replace(b"101,INFY", b"101, infy "), "master_identity_invalid"),
    (MASTER + b"303,INFY,NSE,NSE,EQ,,1,0.05,extra\n", "master_rows_invalid"),
    (MASTER.splitlines()[0] + b"\n101,INFY\n", "master_rows_invalid"),
])
def test_master_guards_refuse_at_the_master_boundary(body, reason):
    with pytest.raises(kh.KiteHistoryError, match=reason):
        feed(Client(master=body))._load_master(NOW)


@pytest.mark.parametrize("row", [None, ["2026-09-16T09:15:00+0530", 100], [17,100,105,95,102,1000]])
def test_candle_shape_reports_its_own_refusal(row):
    with pytest.raises(kh.KiteHistoryError, match="candle_invalid"):
        feed(Client(candles=encoded([row]))).fetch_ohlcv(INSTRUMENT,NOW-timedelta(days=200),NOW,"1d")


def test_missing_master_symbol_and_contract_units_have_specific_refusals():
    for instrument, reason in [(replace(INSTRUMENT,symbol="ABSENT"),"instrument_missing"),
                               (replace(INSTRUMENT,lot_size=10),"instrument_units_mismatch")]:
        with pytest.raises(kh.KiteHistoryError, match=reason):
            feed(Client()).fetch_ohlcv(instrument,NOW-timedelta(days=200),NOW,"1d")


def test_failed_refresh_erases_previous_symbol_evidence():
    client = Client()
    reader = feed(client)
    reader.fetch_ohlcv(INSTRUMENT,NOW-timedelta(days=200),NOW,"1d")
    client.failure = OSError("PRIVATE_RESPONSE")
    with pytest.raises(kh.KiteHistoryError):
        reader.fetch_ohlcv(INSTRUMENT,NOW-timedelta(days=200),NOW,"1d")
    assert reader.evidence() == {}


def test_response_size_refuses_in_get_before_any_decoder(monkeypatch):
    monkeypatch.setattr(kh, "MAX_BYTES", 20)
    with pytest.raises(kh.KiteHistoryError, match="payload_too_large"):
        feed(Client())._get("/instruments/NSE")


def test_decompressed_master_size_refuses_before_csv_validation(monkeypatch):
    import gzip

    body = gzip.compress(MASTER * 100)
    monkeypatch.setattr(kh, "MAX_BYTES", len(body) + 1)
    with pytest.raises(kh.KiteHistoryError, match="payload_too_large"):
        feed(Client(master=body))._load_master(NOW)


def test_duplicate_master_header_has_an_explicit_schema_refusal():
    body = MASTER.replace(b"tick_size",b"lot_size")
    with pytest.raises(kh.KiteHistoryError, match="master_schema_invalid"):
        feed(Client(master=body))._load_master(NOW)


def test_error_envelope_cannot_smuggle_otherwise_valid_candles():
    body = encoded([["2026-09-16T09:15:00+0530",100,105,95,102,1000]])
    body = body.replace(b'"success"',b'"error"')
    with pytest.raises(kh.KiteHistoryError, match="envelope_invalid"):
        feed(Client(candles=body)).fetch_ohlcv(INSTRUMENT,NOW-timedelta(days=200),NOW,"1d")
