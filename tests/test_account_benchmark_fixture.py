import json
from decimal import Decimal
from pathlib import Path

import pytest
from account_benchmark_fixture import fixture


def test_recorded_dated_account_matches_independent_close_mark_oracle(tmp_path):
    result = fixture(tmp_path)
    assert result == json.loads((Path(__file__).parent / "fixtures/account-benchmark.json").read_text())
    assert Decimal(result["account"]["cash_balance"]) == Decimal("9644.39626")
    assert result["expected"][0]["accountEquity"] == 10000
    assert result["expected"][-1]["cash"] == pytest.approx(9644.39626)
    assert sum(row["cashFees"] for row in result["expected"]) == pytest.approx(1.30374)
    assert len(result["expected"]) == 31
    assert not any(p["symbol"] == "INFY" for p in result["positions"])
