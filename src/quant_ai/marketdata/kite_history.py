"""Opt-in, bounded Kite daily-history reader. No order API or credential-file access."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.resilience import (
    HttpResponse,
    ProviderHttpError,
    ResilientHttpClient,
    TokenBucketRateLimiter,
)
from quant_ai.marketdata.instrument_master import parse_instrument_master
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import DailyHistoryProvider, session_close_at, venue_for

IST = ZoneInfo("Asia/Kolkata")
BASE = "https://api.kite.trade"
MAX_BYTES = 10_000_000
MAX_ROWS = 250_000
MAX_CANDLES = 400


class KiteHistoryError(ValueError):
    """Fixed, non-secret diagnostic; upstream bodies/messages are never exposed."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KiteHistoryTransport:
    """GET only, fixed HTTPS origin and history/master paths; refuse redirects."""
    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        parts = urllib.parse.urlsplit(url)
        valid = parts.path == "/instruments/NSE" or re.fullmatch(
            r"/instruments/historical/[1-9][0-9]*/day", parts.path)
        if method != "GET" or parts.scheme != "https" or parts.netloc != "api.kite.trade" or not valid or parts.query or parts.fragment:
            raise KiteHistoryError("kite_history_endpoint_refused")
        query = urllib.parse.urlencode(params or {})
        request = urllib.request.Request(url + ("?" + query if query else ""),
                                         headers=headers or {}, method="GET")
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise KiteHistoryError("kite_history_payload_too_large")
                return HttpResponse(response.status, body, {})
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return HttpResponse(status, b"", {})


def _aware(moment):
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise KiteHistoryError("kite_history_aware_time_required")
    return moment.astimezone(timezone.utc)


def _instrument(instrument):
    if (not isinstance(instrument, Instrument) or instrument.market is not Market.INDIA
            or instrument.exchange != "NSE" or instrument.currency != "INR"
            or instrument.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
            or instrument.tradable is not True or instrument.is_dated_contract
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9&_.-]{0,79}", instrument.symbol)):
        raise KiteHistoryError("kite_history_nse_cash_only")
    return instrument


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KiteHistoryError("kite_history_duplicate_json_key")
        result[key] = value
    return result


def _constant(_value):
    raise KiteHistoryError("kite_history_nonfinite_number")


def _number(value):
    if type(value) not in (int, Decimal):
        raise KiteHistoryError("kite_history_numeric_value_required")
    result = Decimal(value)
    if not result.is_finite():
        raise KiteHistoryError("kite_history_nonfinite_number")
    return result


class KiteDailyFeed:
    """Master-bound cash history. Construction makes no request; nothing is persisted."""
    provider_id = "zerodha-kite-daily-history"

    def __init__(self, api_key: str, access_token: str, *, client=None):
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", v)
               for v in (api_key, access_token)):
            raise KiteHistoryError("kite_history_credentials_required")
        self._headers = {"Authorization": "token " + api_key + ":" + access_token,
                         "X-Kite-Version": "3", "Accept-Encoding": "identity"}
        self.client = client if client is not None else ResilientHttpClient(
            KiteHistoryTransport(), max_attempts=1, max_payload_bytes=MAX_BYTES,
            rate_limiter=TokenBucketRateLimiter(1.0, 1.0))
        self._lock = RLock()
        self._master_day = None
        self._master = None
        self._master_hash = None
        self._state_lock = RLock()
        self._authentication_failed = False
        self._evidence = {}

    @property
    def authentication_rejected(self):
        with self._state_lock:
            return self._authentication_failed

    def _reject_authentication(self):
        with self._state_lock:
            self._authentication_failed = True
            self._evidence.clear()

    def _get(self, path, params=None):
        if self.authentication_rejected:
            raise KiteHistoryError("kite_history_authentication_required")
        try:
            response = self.client.get_response(BASE + path, params=params, headers=self._headers)
        except ProviderHttpError as error:
            if error.status_code in (401, 403):
                self._reject_authentication()
            raise KiteHistoryError("kite_history_http_" + str(error.status_code)) from None
        except Exception:  # noqa: BLE001 - external diagnostics must never enter history logs
            raise KiteHistoryError("kite_history_transport_unavailable") from None
        if response.status_code != 200:
            if response.status_code in (401, 403):
                self._reject_authentication()
            raise KiteHistoryError("kite_history_response_refused")
        if not isinstance(response.body, bytes) or len(response.body) > MAX_BYTES:
            raise KiteHistoryError("kite_history_payload_too_large")
        return response.body

    def _load_master(self, now):
        day = now.astimezone(IST).date()
        if self._master_day == day:
            if self._master is None:
                raise KiteHistoryError("kite_history_master_unavailable")
            return self._master
        self._master_day, self._master = day, None
        body = self._get("/instruments/NSE")
        if body.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as zipped:
                body = zipped.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise KiteHistoryError("kite_history_payload_too_large")
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), strict=True)
        required = {"instrument_token", "tradingsymbol", "exchange", "segment",
                    "instrument_type", "expiry", "lot_size", "tick_size"}
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not required <= set(reader.fieldnames):
            raise KiteHistoryError("kite_history_master_schema_invalid")
        result, tokens = {}, set()
        for index, row in enumerate(reader):
            if index >= MAX_ROWS or None in row or any(v is None for v in row.values()):
                raise KiteHistoryError("kite_history_master_rows_invalid")
            if row["exchange"] != "NSE":
                raise KiteHistoryError("kite_history_master_exchange_invalid")
            if row["instrument_type"] not in {"EQ", "ETF"} or row["segment"] != "NSE":
                continue
            symbol, token = row["tradingsymbol"], row["instrument_token"]
            if row["expiry"] or not re.fullmatch(r"[1-9][0-9]{0,9}", token) or int(token) > 4294967295:
                raise KiteHistoryError("kite_history_master_identity_invalid")
            canonical = parse_instrument_master([row])
            if len(canonical) != 1 or canonical[0]["symbol"] != symbol or canonical[0]["providerInstrumentId"] != token:
                raise KiteHistoryError("kite_history_master_identity_invalid")
            lot, tick = Decimal(row["lot_size"]), Decimal(row["tick_size"])
            if not lot.is_finite() or lot <= 0 or lot != lot.to_integral_value() or not tick.is_finite() or tick <= 0:
                raise KiteHistoryError("kite_history_master_units_invalid")
            if symbol in result or token in tokens:
                raise KiteHistoryError("kite_history_master_duplicate_identity")
            result[symbol] = (token, int(lot), tick)
            tokens.add(token)
        if not result:
            raise KiteHistoryError("kite_history_master_empty")
        self._master_hash = hashlib.sha256(body).hexdigest()
        self._master = result
        return result

    def fetch_ohlcv(self, instrument, start, end, interval):
        _instrument(instrument)
        start, end = _aware(start), _aware(end)
        if interval != "1d" or start >= end or (end - start) > timedelta(days=366):
            raise KiteHistoryError("kite_history_window_invalid")
        with self._lock:
            with self._state_lock:
                self._evidence.pop(instrument.symbol, None)
            try:
                master = self._load_master(end)
                identity = master.get(instrument.symbol)
                if identity is None:
                    raise KiteHistoryError("kite_history_instrument_missing")
                token, lot, tick = identity
                if ((instrument.lot_size is not None and instrument.lot_size != lot)
                        or (instrument.tick_size is not None and instrument.tick_size != tick)):
                    raise KiteHistoryError("kite_history_instrument_units_mismatch")
                local_start, local_end = start.astimezone(IST), end.astimezone(IST)
                params = {"from": local_start.strftime("%Y-%m-%d 00:00:00"),
                          "to": local_end.strftime("%Y-%m-%d %H:%M:%S"), "continuous": "0", "oi": "0"}
                body = self._get("/instruments/historical/" + token + "/day", params)
                payload = json.loads(body, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_pairs)
                if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
                    raise KiteHistoryError("kite_history_envelope_invalid")
                rows = payload["data"].get("candles")
                if not isinstance(rows, list) or len(rows) > MAX_CANDLES:
                    raise KiteHistoryError("kite_history_candles_invalid")
                candles, days, previous = [], set(), None
                for row in rows:
                    if not isinstance(row, list) or len(row) != 6 or not isinstance(row[0], str):
                        raise KiteHistoryError("kite_history_candle_invalid")
                    timestamp = _aware(datetime.fromisoformat(row[0]))
                    day = timestamp.astimezone(IST).date()
                    if (timestamp > end or not local_start.date() <= day <= local_end.date()
                            or day in days or (previous is not None and timestamp <= previous)):
                        raise KiteHistoryError("kite_history_session_invalid")
                    numbers = tuple(_number(value) for value in row[1:])
                    if numbers[-1] != numbers[-1].to_integral_value():
                        raise KiteHistoryError("kite_history_volume_invalid")
                    candle = Candle(instrument, timestamp, *numbers)
                    days.add(day)
                    previous = timestamp
                    if session_close_at(timestamp, venue_for(instrument.market)) <= end:
                        candles.append(candle)
                with self._state_lock:
                    self._evidence[instrument.symbol] = {
                        "source": self.provider_id, "observedAt": end.isoformat(),
                        "masterSha256": self._master_hash, "responseSha256": hashlib.sha256(body).hexdigest(),
                        "instrumentToken": token, "receivedRows": len(rows), "closedRows": len(candles),
                        "priceBasis": "provider_supplied_adjustment_basis_not_independently_verified"}
                return tuple(candles)
            except KiteHistoryError:
                raise
            except (ValueError, TypeError, OSError, RuntimeError, LookupError, ArithmeticError, csv.Error, EOFError):
                raise KiteHistoryError("kite_history_invalid_data") from None

    def evidence(self):
        # This lock is never held during provider I/O.
        with self._state_lock:
            if self._authentication_failed:
                return {}
            return {symbol: dict(record) for symbol, record in self._evidence.items()}


class KiteDailyHistoryProvider(DailyHistoryProvider):
    """Reuse the closed-session/window behavior with exact-identity daily caching."""
    provider_id = "zerodha-kite-daily-history"

    def __init__(self, api_key, access_token, *, client=None):
        feed = KiteDailyFeed(api_key, access_token, client=client)
        super().__init__(feed.client, feed=feed)
        self._observations = {}
        self._cache_lock = RLock()
        self._fetch_lock = RLock()

    @staticmethod
    def _identity(instrument):
        _instrument(instrument)
        return (instrument.symbol, instrument.market, instrument.asset_class, instrument.currency,
                instrument.exchange, instrument.lot_size, instrument.tick_size,
                instrument.underlying, tuple(sorted(instrument.metadata.items())))

    def fetch(self, instrument, now):
        current, key = _aware(now), self._identity(instrument)
        # Never hold the cache lock during provider I/O: protection only reads cache.
        with self._fetch_lock:
            with self.feed._state_lock, self._cache_lock:
                if self.feed._authentication_failed:
                    self._observations.clear()
                    return ()
                held = self._observations.get(key)
                if held and held[0].date() == current.date():
                    return held[1] if held[0] <= current else ()
            result = self._fetch_uncached(instrument, current)
            with self.feed._state_lock, self._cache_lock:
                if self.feed._authentication_failed:
                    self._observations.clear()
                    return ()
                self._observations[key] = (current, result)
                return result

    def cached(self, instrument, now):
        current, key = _aware(now), self._identity(instrument)
        with self.feed._state_lock, self._cache_lock:
            if self.feed._authentication_failed:
                self._observations.clear()
                return ()
            held = self._observations.get(key)
            return held[1] if held and held[0].date() == current.date() and held[0] <= current else ()

    def evidence(self):
        return self.feed.evidence()
