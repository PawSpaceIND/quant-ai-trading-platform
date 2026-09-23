"""The 12-name NSE universe is committed config the host selects through .env, never a new default."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from test_pilot_required_risk_gates import daily

from quant_ai.domain.models import AssetClass, Market
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.pilot import validate_pilot_instruments
from quant_ai.intelligence.external.rss import parse_symbol_aliases, symbol_aliases_from_env
from quant_ai.risk.book_history import normalize_sector_map

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
NOW = datetime(2026, 9, 17, 5, 0, tzinfo=timezone.utc)

EQUITIES = ("TRENT", "BEL", "BAJAJ-AUTO", "INDIGO", "NTPC", "COALINDIA",
            "BHARTIARTL", "SUNPHARMA", "LT", "EICHERMOT")
ETFS = ("GOLDBEES", "SILVERBEES")
UNIVERSE = EQUITIES + ETFS
SECTORS = {
    "TRENT": "RETAIL", "BEL": "DEFENCE_ELECTRONICS", "BAJAJ-AUTO": "AUTOMOBILES",
    "INDIGO": "AVIATION", "NTPC": "POWER_UTILITIES", "COALINDIA": "MINING",
    "BHARTIARTL": "TELECOM", "SUNPHARMA": "PHARMACEUTICALS", "LT": "ENGINEERING_CONSTRUCTION",
    "EICHERMOT": "AUTOMOBILES", "GOLDBEES": "PRECIOUS_METALS", "SILVERBEES": "PRECIOUS_METALS",
}
EXAMPLE_UNIVERSE = ("INFY", "TCS", "RELIANCE", "GOLDBEES", "SILVERBEES")

DIRECTIVES_FILE = "founder-directives.top10.json"
SECTOR_MAP_FILE = "pilot-sector-map.top10.json"
ALIASES_FILE = "news-symbol-aliases.top10.json"


def read(name):
    return json.loads((DEPLOY / name).read_text(encoding="utf-8"))


def preflight():
    spec = importlib.util.spec_from_file_location(
        "top10_preflight", ROOT / "scripts/check_pilot_risk_gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_top10_directives_load_and_pass_pilot_validation():
    directives = FounderDirectives.from_json(read(DIRECTIVES_FILE))
    validate_pilot_instruments(directives.watchlist, now=NOW)

    symbols = tuple(item.symbol for item in directives.watchlist)
    assert symbols == UNIVERSE
    assert len(symbols) == 12 and len(set(symbols)) == 12
    equities = tuple(i.symbol for i in directives.watchlist if i.asset_class is AssetClass.EQUITY)
    etfs = tuple(i.symbol for i in directives.watchlist if i.asset_class is AssetClass.ETF)
    assert equities == EQUITIES and etfs == ETFS
    for instrument in directives.watchlist:
        assert instrument.market is Market.INDIA
        assert instrument.exchange == "NSE" and instrument.currency == "INR"
        assert instrument.tradable and instrument.expiry is None


def test_top10_validation_still_demands_a_timezone_aware_clock():
    directives = FounderDirectives.from_json(read(DIRECTIVES_FILE))
    with pytest.raises(ValueError, match="timezone_aware"):
        validate_pilot_instruments(directives.watchlist, now=NOW.replace(tzinfo=None))


def test_top10_directives_change_only_the_watchlist_and_the_capital():
    # Every other field is the example's, verbatim: same posture, caps and instructions.
    # A universe file that quietly moved the position cap would be a second change hiding
    # inside the first. The capital is the one named exception: on 23 September 2026 the
    # founder raised the pilot to ten lakh so one share of every name fits inside the 5%
    # trade cap, and the ledger was topped up to match through a recorded contribution.
    example, top10 = read("founder-directives.example.json"), read(DIRECTIVES_FILE)
    assert set(top10) == set(example)
    for key in example:
        if key not in {"watchlist", "starting_capital"}:
            assert top10[key] == example[key], key
    assert top10["starting_capital"] == 1_000_000 and top10["max_open_positions"] == 5
    assert set(top10["allowed_asset_classes"]) == {"EQUITY", "ETF"}
    assert top10["allowed_markets"] == ["INDIA"]
    shape = tuple(sorted(example["watchlist"][0]))
    assert {tuple(sorted(row)) for row in top10["watchlist"]} == {shape}


def test_top10_sector_map_covers_the_watchlist_exactly():
    raw = read(SECTOR_MAP_FILE)
    normalized = normalize_sector_map(raw)
    assert normalized == raw, "the committed file must already be in normalized form"
    assert normalized == SECTORS
    assert set(normalized) == set(UNIVERSE)
    assert normalized["BAJAJ-AUTO"] == normalized["EICHERMOT"] == "AUTOMOBILES"
    assert normalized["GOLDBEES"] == normalized["SILVERBEES"] == "PRECIOUS_METALS"


def test_top10_universe_arms_every_required_risk_gate():
    directives = FounderDirectives.from_json(read(DIRECTIVES_FILE))
    sectors = read(SECTOR_MAP_FILE)
    module = preflight()

    report = module.check(directives, sectors, daily(), NOW)
    assert report["allArmed"] and report["dataReady"] and report["paperOnly"]
    assert report["hostAcceptance"] is False
    assert report["symbols"] == list(UNIVERSE)
    assert report["sectorRecords"] == 12 and report["sectorGroups"] == 10
    assert report["history"]["records"] == 12 * 120

    # One unmapped watchlist symbol is enough to leave the gates unarmed.
    short = {symbol: group for symbol, group in sectors.items() if symbol != "BAJAJ-AUTO"}
    assert module.check(directives, short, daily(), NOW)["allArmed"] is False


def test_top10_news_aliases_parse_and_name_every_symbol():
    raw = read(ALIASES_FILE)
    parsed = parse_symbol_aliases(raw)

    assert set(parsed) == set(UNIVERSE)
    for symbol, aliases in parsed.items():
        assert aliases, symbol
        assert len(aliases) == len(set(aliases)), symbol
        for alias in aliases:
            assert alias not in UNIVERSE, f"{symbol}: {alias} is a watchlist symbol, not an alias"
    # The runtime never reads the file; the operator pastes it as one env line.
    one_line = json.dumps(raw, separators=(",", ":"))
    assert "\n" not in one_line
    assert symbol_aliases_from_env(one_line) == parsed


def test_the_five_name_example_files_are_untouched():
    example = FounderDirectives.from_json(read("founder-directives.example.json"))
    validate_pilot_instruments(example.watchlist, now=NOW)
    assert tuple(item.symbol for item in example.watchlist) == EXAMPLE_UNIVERSE
    sectors = normalize_sector_map(read("pilot-sector-map.example.json"))
    assert set(sectors) == set(EXAMPLE_UNIVERSE)


def test_compose_defaults_still_mount_the_example_files():
    text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "top10" not in text, "the universe switch lives in .env, not in the compose file"
    ghost = yaml.safe_load(text)["services"]["pramana-ghost"]
    mounts = {row["target"]: row for row in ghost["volumes"] if isinstance(row, dict)}
    directives = mounts["/app/directives.json"]
    sectors = mounts[ghost["environment"]["PRAMANA_SECTOR_MAP_FILE"]]
    assert directives["source"] == "${PRAMANA_DIRECTIVES_HOST_FILE:-./founder-directives.example.json}"
    assert sectors["source"] == "${PRAMANA_SECTOR_MAP_HOST_FILE:-./pilot-sector-map.example.json}"
    for mount in (directives, sectors):
        assert mount["read_only"] is True and mount["bind"]["create_host_path"] is False
    aliases = ghost["environment"]["PRAMANA_NEWS_SYMBOL_ALIASES_JSON"]
    assert aliases == "${PRAMANA_NEWS_SYMBOL_ALIASES_JSON:-}"


def test_env_example_documents_the_switch_as_comments_only():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    expected = (
        "# PRAMANA_DIRECTIVES_HOST_FILE=./founder-directives.top10.json",
        "# PRAMANA_SECTOR_MAP_HOST_FILE=./pilot-sector-map.top10.json",
        ("# PRAMANA_NEWS_SYMBOL_ALIASES_JSON="
         "'<contents of deploy/news-symbol-aliases.top10.json on one line>'"),
    )
    for line in expected:
        assert line in lines, line
    switches = {"PRAMANA_DIRECTIVES_HOST_FILE", "PRAMANA_SECTOR_MAP_HOST_FILE",
                "PRAMANA_NEWS_SYMBOL_ALIASES_JSON"}
    live = [line for line in lines if line.split("=")[0] in switches]
    assert live == [], "the example must not switch the universe by itself"
