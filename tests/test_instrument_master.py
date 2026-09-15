import json
from datetime import datetime, timezone

from quant_ai.marketdata.instrument_master import instrument_universe, parse_instrument_master


def test_master_parser_keeps_exact_contract_metadata_and_supported_india_venues():
    payload = """instrument_token,exchange,tradingsymbol,name,expiry,strike,tick_size,lot_size,instrument_type,segment
1,NSE,INFY,Infosys,,,0.05,1,EQ,NSE
2,BSE,RELIANCE,Reliance,,,0.05,1,EQ,BSE
3,NFO,NIFTY26SEP25000CE,NIFTY,2026-09-24,25000,0.05,75,CE,NFO-OPT
4,MCX,GOLDM26DEC,GOLD,2026-12-05,0,0.1,100,FUT,MCX-FUT
5,BCD,USDINR26SEP,USDINR,2026-09-28,0,0.0025,1000,FUT,BCD-FUT
6,NASDAQ,AAPL,Apple,,,0.01,1,EQ,NASDAQ
"""
    rows = parse_instrument_master(payload)
    assert [(row["exchange"], row["symbol"]) for row in rows] == [
        ("NSE", "INFY"), ("BSE", "RELIANCE"), ("NFO", "NIFTY26SEP25000CE"),
        ("MCX", "GOLDM26DEC"), ("BCD", "USDINR26SEP"),
    ]
    option = rows[2]
    assert option["assetClass"] == "OPTION"
    assert option["expiry"] == "2026-09-24"
    assert option["strike"] == 25000.0
    assert option["optionType"] == "CE"
    assert option["lotSize"] == 75.0
    assert option["shortability"] == "broker_rules_required"


def test_instrument_universe_uses_file_and_returns_counts(tmp_path, monkeypatch):
    path = tmp_path / "instruments.csv"
    path.write_text("instrument_token,exchange,tradingsymbol,name,expiry,strike,tick_size,lot_size,instrument_type,segment\n1,NSE,INFY,Infosys,,,0.05,1,EQ,NSE\n2,MCX,GOLDM26DEC,GOLD,2026-12-05,0,0.1,100,FUT,MCX-FUT\n")
    monkeypatch.setenv("PRAMANA_ZERODHA_INSTRUMENTS_FILE", str(path))
    result = instrument_universe(object(), datetime.now(timezone.utc), tmp_path)
    assert result["status"] == "available"
    assert result["total"] == 2
    assert result["byExchange"] == {"MCX": 1, "NSE": 1}
    assert result["byAssetClass"]["METAL"] == 1
    assert result["derivativeContracts"] == 1
    assert result["recordsPath"].endswith("instrument-universe.normalized.json")
    assert (tmp_path / "instrument-universe.normalized.json").exists()
    json.dumps(result)


def test_instrument_universe_refreshes_kite_once_then_uses_cache(tmp_path):
    class FakeKite:
        calls = 0

        def instruments(self):
            self.calls += 1
            return [{
                "instrument_token": 1, "exchange_token": 2, "exchange": "BSE",
                "tradingsymbol": "RELIANCE", "name": "Reliance",
                "instrument_type": "EQ", "segment": "BSE",
            }]

    kite = FakeKite()
    first = instrument_universe(kite, datetime.now(timezone.utc), tmp_path)
    second = instrument_universe(kite, datetime.now(timezone.utc), tmp_path)
    assert first["status"] == second["status"] == "available"
    assert first["total"] == second["total"] == 1
    assert kite.calls == 1
