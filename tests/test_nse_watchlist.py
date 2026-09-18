"""Synthetic-only fifty-name preparation, strict identity and actual firewall tests."""
from __future__ import annotations

import copy
import csv
import importlib.util
import io
import json
import socket
import stat
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_portfolio_risk_controls import book

from quant_ai import daemon
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.governance import nse_watchlist as nse
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.pilot import validate_pilot_instruments
from quant_ai.risk.policy import BookRiskFirewall

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 18, 6, tzinfo=timezone.utc)
EQUITIES = tuple(nse.BASE_SYMBOLS[:3]) + tuple(f"NSEFIX{i:03d}" for i in range(47))
ALL = EQUITIES + tuple(nse.BASE_SYMBOLS[3:])


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("Unexpected network request")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")


def csv_bytes(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def constituents_rows():
    return [{"Company Name": "SYNTHETIC " + s, "Industry": "FIXTURE SECTOR " + str(i % 10),
             "Symbol": s, "Series": "EQ", "ISIN Code": f"IN{i:010d}"}
            for i, s in enumerate(EQUITIES)]


def master_rows():
    return [dict(zip(nse.MASTER_COLUMNS,
                (str(100_000 + i), str(200_000 + i), s, "SYNTHETIC", "0", "", "0",
                 "0.05", "1", "EQ", "NSE", "NSE"))) for i, s in enumerate(ALL)]


def base():
    return json.loads((ROOT / "deploy/founder-directives.example.json").read_text())


def prepare(**overrides):
    args = {"base": base(), "constituents": csv_bytes(nse.CONSTITUENT_COLUMNS, constituents_rows()),
            "master": csv_bytes(nse.MASTER_COLUMNS, master_rows()), "now": NOW,
            "constituents_at": NOW, "master_at": NOW, "daily_call_limit": 500,
            "daily_token_limit": 2_000_000}
    args.update(overrides)
    return nse.prepare_bundle(**args)


def mapping_fixture():
    proposal = prepare()
    instruments = FounderDirectives.from_json(proposal["directives"]).watchlist
    tokens = json.loads(proposal["environment"]["PRAMANA_ZERODHA_TOKENS_JSON"])
    mapping = {int(k): v for k, v in json.loads(
        proposal["environment"]["PRAMANA_ZERODHA_SYMBOLS_JSON"]).items()}
    return instruments, tokens, mapping


def test_prepares_fifty_preserves_base_and_does_not_claim_host_acceptance():
    initial = base()
    frozen = copy.deepcopy(initial)
    proposal = prepare(base=initial)
    assert initial == frozen
    directives = proposal["directives"]
    assert len(directives["watchlist"]) == 50
    assert [i["symbol"] for i in directives["watchlist"]][:5] == list(nse.BASE_SYMBOLS)
    assert sum(i["asset_class"] == "ETF" for i in directives["watchlist"]) == 2
    for key in frozen.keys() - {"watchlist", "sector_map"}:
        assert directives[key] == frozen[key]
    assert proposal["hostAcceptance"] is False
    assert proposal["status"] == "prepared_offline"
    assert "operator_review_required" in proposal["sourceQualification"]
    assert not any("ACCESS_TOKEN" in k for k in proposal["environment"])
    assert proposal["selection"]["excludedConstituents"]
    assert proposal["workload"]["budgetChanged"] is False
    for item in directives["watchlist"]:
        assert item["lot_size"] == 1 and Decimal(item["tick_size"]) == Decimal("0.05")
    validate_pilot_instruments(FounderDirectives.from_json(directives).watchlist)


def test_selection_is_deterministic_and_preserves_all_source_groups():
    sectors = nse.parse_constituents(csv_bytes(nse.CONSTITUENT_COLUMNS, constituents_rows()))
    chosen = nse.select_equities(sectors)
    assert chosen == nse.select_equities(dict(reversed(list(sectors.items()))))
    assert len(chosen) == 48 and nse.RETAINED_EQUITIES <= set(chosen)
    assert {sectors[s] for s in chosen} == set(sectors.values())


@pytest.mark.parametrize("field,delta,code", [
    ("master_at", -timedelta(hours=24, seconds=1), "watchlist_source_stale_or_future"),
    ("constituents_at", -timedelta(days=7, seconds=1), "watchlist_source_stale_or_future"),
    ("master_at", timedelta(seconds=1), "watchlist_source_stale_or_future"),
    ("constituents_at", timedelta(seconds=1), "watchlist_source_stale_or_future"),
])
def test_source_age_refusal(field, delta, code):
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        prepare(**{field: NOW + delta})


@pytest.mark.parametrize("field", ["now", "master_at", "constituents_at"])
def test_source_clock_must_be_aware(field):
    with pytest.raises(nse.WatchlistPreparationError, match="aware_timestamp_required"):
        prepare(**{field: NOW.replace(tzinfo=None)})


@pytest.mark.parametrize("case,code", [
    ("header", "source_header_invalid"), ("extra_row", "source_row_limit"),
    ("missing_row", "constituent_count_not_fifty"), ("missing_cell", "source_row_invalid"),
    ("symbol", "symbol_invalid"), ("series", "constituent_not_cash_equity"),
    ("company", "constituent_not_cash_equity"), ("sector", "sector_invalid"),
    ("isin", "isin_invalid"), ("duplicate", "constituent_duplicate"),
    ("duplicate_isin", "constituent_duplicate"), ("retained", "retained_equity_missing"),
    ("etf", "constituent_etf_conflict"), ("oversize", "source_size_invalid"),
    ("empty", "source_size_invalid"), ("no_rows", "source_empty"),
])
def test_constituent_refusals(case, code):
    rows = constituents_rows()
    columns = nse.CONSTITUENT_COLUMNS
    if case == "header": columns = columns[::-1]
    elif case == "extra_row": rows.append(rows[-1])
    elif case == "missing_row": rows.pop()
    elif case == "symbol": rows[-1]["Symbol"] = "BAD\nSYMBOL"
    elif case == "series": rows[-1]["Series"] = "BE"
    elif case == "company": rows[-1]["Company Name"] = ""
    elif case == "sector": rows[-1]["Industry"] = ""
    elif case == "isin": rows[-1]["ISIN Code"] = "bad"
    elif case == "duplicate": rows[-1]["Symbol"] = rows[-2]["Symbol"]
    elif case == "duplicate_isin": rows[-1]["ISIN Code"] = rows[-2]["ISIN Code"]
    elif case == "retained": rows[0]["Symbol"] = "OTHERFIXTURE"
    elif case == "etf": rows[-1]["Symbol"] = "GOLDBEES"
    elif case == "no_rows": rows = []
    payload = csv_bytes(columns, rows)
    if case == "missing_cell": payload = payload.rsplit(b",", 1)[0] + b"\n"
    elif case == "oversize": payload = b"x" * 200_001
    elif case == "empty": payload = b""
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        nse.parse_constituents(payload)


def test_selection_rejects_missing_retained_or_unique_groups():
    with pytest.raises(nse.WatchlistPreparationError, match="selection_scope_invalid"):
        nse.select_equities({})
    with pytest.raises(nse.WatchlistPreparationError, match="selection_cannot_preserve_groups"):
        nse.select_equities(dict(zip(EQUITIES, EQUITIES)))


@pytest.mark.parametrize("field,value,code", [
    ("segment", "NFO", "master_not_cash"), ("instrument_type", "FUT", "master_not_cash"),
    ("expiry", "2026-09-30", "master_not_cash"), ("strike", "10", "master_not_cash"),
    ("lot_size", "2", "master_cash_lot_invalid"),
    ("instrument_token", "0", "master_token_invalid"),
    ("instrument_token", "True", "master_token_invalid"),
    ("instrument_token", "010", "master_token_invalid"),
    ("instrument_token", "4294967296", "master_token_invalid"),
    ("tick_size", "NaN", "master_tick_invalid"), ("tick_size", "Infinity", "master_tick_invalid"),
    ("tick_size", "0", "master_tick_invalid"), ("tick_size", "-1", "master_tick_invalid"),
    ("tick_size", "bad", "master_tick_invalid"),
])
def test_master_row_refusals(field, value, code):
    rows = master_rows()
    rows[0][field] = value
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        nse.derive_master_identities(csv_bytes(nse.MASTER_COLUMNS, rows), ALL)


@pytest.mark.parametrize("case,code", [
    ("missing", "symbol_without_master_token"), ("exchange", "symbol_without_master_token"),
    ("ambiguous", "master_ambiguous_symbol"), ("duplicate_token", "master_duplicate_token"),
    ("token_alias", "master_token_alias"),
])
def test_master_scope_refusals(case, code):
    rows = master_rows()
    if case == "missing": rows.pop()
    elif case == "exchange": rows[0]["exchange"] = "BSE"
    elif case == "ambiguous": rows.append(rows[0])
    elif case == "duplicate_token": rows[1]["instrument_token"] = rows[0]["instrument_token"]
    else:
        row = dict(rows[0], tradingsymbol="UNSELECTED")
        rows.append(row)
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        nse.derive_master_identities(csv_bytes(nse.MASTER_COLUMNS, rows), ALL)


def test_existing_normalizer_reused_and_identity_conflict_refuses(monkeypatch):
    called = []
    original = nse.normalize_row
    def normal(row):
        called.append(row["tradingsymbol"])
        return original(row)
    monkeypatch.setattr(nse, "normalize_row", normal)
    found = nse.derive_master_identities(csv_bytes(nse.MASTER_COLUMNS, master_rows()), ALL)
    assert set(called) == set(ALL) and len(found) == 52
    monkeypatch.setattr(nse, "normalize_row", lambda row: None)
    with pytest.raises(nse.WatchlistPreparationError, match="normalization_mismatch"):
        nse.derive_master_identities(csv_bytes(nse.MASTER_COLUMNS, master_rows()), ALL)


@pytest.mark.parametrize("case,code", [
    ("extra_token", "token_without_symbol_mapping"),
    ("missing_mapping", "token_without_symbol_mapping"),
    ("missing_symbol", "symbol_without_subscription_token"),
    ("duplicate_token", "subscription_duplicate_token"),
    ("duplicate_symbol", "subscription_duplicate_symbol"),
    ("token_type", "subscription_token_invalid"), ("key_type", "subscription_key_invalid"),
    ("symbol_type", "subscription_symbol_invalid"),
])
def test_subscription_bijection_refuses(case, code):
    instruments, tokens, mapping = mapping_fixture()
    if case == "extra_token": tokens.append(900_001)
    elif case == "missing_mapping": mapping.pop(tokens[0])
    elif case == "missing_symbol":
        mapping.pop(tokens[0]); tokens.pop(0)
    elif case == "duplicate_token": tokens.append(tokens[0])
    elif case == "duplicate_symbol": mapping[tokens[0]] = mapping[tokens[1]]
    elif case == "token_type": tokens[0] = True
    elif case == "key_type": mapping[str(tokens[0])] = mapping.pop(tokens[0])
    elif case == "symbol_type": mapping[tokens[0]] = "not canonical"
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        nse.validate_subscription_mapping(instruments, tokens, mapping)


def test_fifty_first_name_refused_by_unchanged_pilot_cap():
    instruments, tokens, mapping = mapping_fixture()
    extra = Instrument("EXTRAFIX", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    with pytest.raises(ValueError, match="pilot_watchlist_limit"):
        nse.validate_subscription_mapping((*instruments, extra), [*tokens, 900_001],
                                          {**mapping, 900_001: extra.symbol})


@pytest.mark.parametrize("case,code", [("missing", "sector_scope_incomplete"),
                                       ("one_sector", "sector_spread_insufficient")])
def test_sector_spread_refused(case, code):
    symbols = list(ALL[:50])
    sectors = dict.fromkeys(symbols, "ONE SECTOR")
    if case == "missing": sectors.pop(symbols[0])
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        nse.validate_sector_spread(symbols, sectors)


def test_actual_item_two_firewall_refuses_a_concentrated_book():
    risk = BookRiskFirewall(sector_map=dict.fromkeys(ALL, "ONE SECTOR"))
    order = OrderIntent("TCS", Market.INDIA, Side.BUY, 100, Decimal(100), "fixture")
    decision = risk.evaluate(order, book({"INFY": Decimal(20000)}))
    assert not decision.approved and "sector" in decision.reason


def test_expanded_boot_mapping_refusal_precedes_any_broker_write(tmp_path, monkeypatch):
    proposal = prepare()
    directives = FounderDirectives.from_json(proposal["directives"])
    calls = []
    def forbidden(*a, **k):
        calls.append("broker")
        pytest.fail("Malformed expansion reached persistent broker construction")
    monkeypatch.setattr(daemon, "PaperBrokerService", forbidden)
    with pytest.raises(nse.WatchlistPreparationError, match="symbol_without_subscription_token"):
        daemon.build_ghost_runner(zerodha_api_key="test", zerodha_access_token="test",
            zerodha_instrument_tokens=(), zerodha_symbol_by_token={}, ib_client=SimpleNamespace(),
            ib_contracts=(), include_ibkr=False, directives=directives,
            database=tmp_path / "never.db", pilot_mode=True)
    assert not calls and not (tmp_path / "never.db").exists()


@pytest.mark.parametrize("case,code", [("base", "expected_five_name_base"),
    ("asset", "base_asset_class_invalid"), ("lot", "existing_lot_conflict"),
    ("tick", "existing_tick_conflict")])
def test_base_identity_is_not_silently_changed(case, code):
    original = base()
    if case == "base": original["watchlist"] = original["watchlist"][::-1]
    elif case == "asset": original["watchlist"][0]["asset_class"] = "ETF"
    elif case == "lot": original["watchlist"][0]["lot_size"] = 5
    else: original["watchlist"][0]["tick_size"] = "0.10"
    with pytest.raises(nse.WatchlistPreparationError, match=code):
        prepare(base=original)


def test_workload_uses_calendar_and_reports_budget_shortfall_without_currency_guess():
    report = nse.workload_report(date(2026, 9, 18), 500, 2_000_000)
    assert report["scheduledRegularTicks"] == 37
    assert report["fiveNameConsensusOpportunities"] == 185
    assert report["fiftyNameConsensusOpportunities"] == 1850
    assert report["maxCompleteFiftyNameSweepsByCalls"] == 10
    assert report["callLimitCoversFiftyConsensus"] is False
    assert report["currencyCost"] is None and report["actualTokenDemand"] is None
    assert report["budgetChanged"] is False
    assert nse.workload_report(date(2026, 9, 19), 500, 2_000_000)["scheduledRegularTicks"] == 0
    assert nse.workload_report(date(2026, 9, 14), 500, 2_000_000)["scheduledRegularTicks"] == 0


@pytest.mark.parametrize("calls,tokens", [(0, 10), (10, 0), (-1, 10), (True, 10), (10, True)])
def test_disabled_or_ambiguous_budget_refuses(calls, tokens):
    with pytest.raises(nse.WatchlistPreparationError, match="budget_must_remain_enabled"):
        nse.workload_report(NOW.date(), calls, tokens)


def cli_module():
    spec = importlib.util.spec_from_file_location("prepare_nse_cli", ROOT / "scripts/prepare_nse_watchlist.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cli_args(tmp_path):
    (tmp_path / "base.json").write_text(json.dumps(base()))
    (tmp_path / "constituents.csv").write_bytes(csv_bytes(nse.CONSTITUENT_COLUMNS, constituents_rows()))
    (tmp_path / "master.csv").write_bytes(csv_bytes(nse.MASTER_COLUMNS, master_rows()))
    at = datetime.now(timezone.utc).isoformat()
    return ["--base-directives", str(tmp_path / "base.json"),
            "--constituents", str(tmp_path / "constituents.csv"), "--constituents-observed-at", at,
            "--master", str(tmp_path / "master.csv"), "--master-observed-at", at,
            "--daily-call-limit", "500", "--daily-token-limit", "2000000",
            "--output-directory", str(tmp_path / "new-proposal")]


def test_cli_creates_private_proposal_and_refuses_overwrite(tmp_path, capsys):
    args = cli_args(tmp_path)
    cli = cli_module()
    assert cli.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PROPOSAL_PREPARED_NOT_DEPLOYED"
    assert output["hostAcceptance"] is False
    directory = tmp_path / "new-proposal"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in directory.iterdir())
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert cli.main(args) == 1
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert not (tmp_path / ".env").exists()


def test_cli_refuses_invalid_input_without_echoing_it(tmp_path, capsys):
    args = cli_args(tmp_path)
    marker = "SYNTHETIC_PRIVATE_INPUT"
    (tmp_path / "base.json").write_text('{"value": "' + marker + '", "value": 2}')
    assert cli_module().main(args) == 1
    assert marker not in capsys.readouterr().out
    assert not (tmp_path / "new-proposal").exists()


def test_master_requires_nonempty_unique_scope():
    payload = csv_bytes(nse.MASTER_COLUMNS, master_rows())
    for symbols in ((), ("INFY", "INFY")):
        with pytest.raises(nse.WatchlistPreparationError, match="master_scope_invalid"):
            nse.derive_master_identities(payload, symbols)


def test_workload_day_must_not_include_time():
    with pytest.raises(nse.WatchlistPreparationError, match="workload_day_invalid"):
        nse.workload_report(NOW, 500, 2_000_000)


def test_final_count_checked_independently(monkeypatch):
    select = nse.select_equities
    def too_few(sectors):
        selected = select(sectors)
        removed = next(s for s in selected if s not in nse.RETAINED_EQUITIES)
        return tuple(s for s in selected if s != removed)
    monkeypatch.setattr(nse, "select_equities", too_few)
    with pytest.raises(nse.WatchlistPreparationError, match="expansion_not_fifty"):
        prepare()


def test_sector_count_cap_is_not_only_a_group_count():
    sectors = {s: "SMALL" + str(i % 7) for i, s in enumerate(ALL[:50])}
    for symbol in ALL[:13]: sectors[symbol] = "LARGE"
    with pytest.raises(nse.WatchlistPreparationError, match="sector_spread_insufficient"):
        nse.validate_sector_spread(ALL[:50], sectors)


def test_csv_refuses_invalid_encoding_and_extra_cells():
    for payload in (b"\xff", csv_bytes(nse.CONSTITUENT_COLUMNS, constituents_rows()) + b"extra,,,,,field\n"):
        with pytest.raises(nse.WatchlistPreparationError):
            nse.parse_constituents(payload)


def test_cli_bounded_reader_and_duplicate_json_guard(tmp_path):
    cli = cli_module()
    sample = tmp_path / "input"
    sample.write_bytes(b"x" * 5)
    with pytest.raises(nse.WatchlistPreparationError, match="input_too_large"):
        cli.read_bounded(sample, 4)
    with pytest.raises(nse.WatchlistPreparationError, match="duplicate_json_key"):
        cli.unique_pairs([("same", 1), ("same", 2)])


def test_no_added_rate_cost_budget_or_cap_change():
    from quant_ai.llm.budget import DEFAULT_DAILY_CALL_LIMIT, DEFAULT_DAILY_TOKEN_LIMIT
    from quant_ai.operations.health import MAX_WATCHLIST
    assert MAX_WATCHLIST == 64
    assert DEFAULT_DAILY_CALL_LIMIT == 500 and DEFAULT_DAILY_TOKEN_LIMIT == 2_000_000
    report = prepare()
    assert report["workload"]["currencyCost"] is None
    assert report["directives"]["max_open_positions"] == base()["max_open_positions"]


def test_cli_refuses_existing_empty_output_directory(tmp_path, capsys):
    args = cli_args(tmp_path)
    target = tmp_path / "new-proposal"
    target.mkdir()
    assert cli_module().main(args) == 1
    assert list(target.iterdir()) == []


def test_cli_removes_inherited_setgid_from_new_private_directory(tmp_path, monkeypatch):
    args = cli_args(tmp_path)
    tmp_path.chmod(0o2700)
    target = tmp_path / "new-proposal"
    real_mkdir = Path.mkdir

    def mkdir_with_inherited_setgid(self, mode=0o777, parents=False, exist_ok=False):
        real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)
        if self == target:
            self.chmod(0o2700)

    # Some filesystems do not propagate a parent's setgid bit. Simulate the
    # inherited state explicitly so removal of the production chmod is detected
    # on every supported CI platform.
    monkeypatch.setattr(Path, "mkdir", mkdir_with_inherited_setgid)
    assert cli_module().main(args) == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o2700


def test_master_refuses_noncanonical_unselected_token_alias():
    rows = master_rows()
    rows.append(dict(rows[0], tradingsymbol="ALIASFIX", instrument_token="0" + rows[0]["instrument_token"]))
    with pytest.raises(nse.WatchlistPreparationError, match="master_token_invalid"):
        nse.derive_master_identities(csv_bytes(nse.MASTER_COLUMNS, rows), ALL)
