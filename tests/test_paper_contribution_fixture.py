import json
from decimal import Decimal
from pathlib import Path

from paper_contribution_fixture import fixture


def test_real_paper_account_arithmetic_and_shared_fixture(tmp_path):
    result = fixture(tmp_path)
    expected = json.loads((Path(__file__).parent / "fixtures" / "paper-contribution.json").read_text())
    assert result == expected
    assert result["reconciliation"]["status"] == "matched"
    fresh = result["valuations"]["fresh"]
    # Fill average excludes fees: (4*101.1 + 2*121.32)/6 = 107.84.
    tcs = next(p for p in fresh["holdings"] if p["symbol"] == "TCS")
    assert tcs["quantity"] == 3 and Decimal(str(tcs["averageEntry"])) == Decimal("107.84")
    assert Decimal(str(tcs["unrealizedPnl"])) == Decimal("51.48")
    assert Decimal(result["account"]["cash_balance"]) == Decimal("9644.39626")
    assert Decimal(str(fresh["realizedPnl"])) == Decimal("48.79626")
    assert Decimal(str(fresh["unrealizedPnl"])) == Decimal("49.6")
    assert Decimal(str(fresh["totalEquity"])) == Decimal("10098.39626")
    assert not any(p["symbol"] == "INFY" for p in fresh["holdings"])
    assert result["valuations"]["stale"]["status"] == "degraded"
    partial = result["valuations"]["partial"]
    assert partial["status"] == "degraded"
    assert {p["symbol"]: p["fresh"] for p in partial["holdings"]} == {"NIFTY": False, "TCS": True}
