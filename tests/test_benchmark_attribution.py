from copy import deepcopy
from decimal import Decimal

import pytest

from quant_ai.analytics.benchmark_attribution import benchmark_attribution


def fixture():
    return {"schema": "pramana.benchmark_attribution_input.v1", "currency": "INR",
            "basis": "beginning_weights_same_period_total_returns", "portfolioReturn": "0.03", "benchmarkReturn": "0.0375",
            "sectors": [dict(zip(("name", "portfolioWeight", "benchmarkWeight", "portfolioReturn", "benchmarkReturn"), row)) for row in [
                ("Materials", ".25", ".20", ".06", ".08"), ("Industrials", ".25", ".15", ".07", ".07"),
                ("Energy", ".25", ".25", "-.04", "-.02"), ("Financials", ".25", ".40", ".03", ".04")]]}


def test_published_four_sector_example_reconciles():
    # Achmea Investment Management, Shapley Attribution, Table 8.
    report = benchmark_attribution(fixture())
    assert {k: Decimal(v) for k, v in report["totals"].items()} == {
        "allocation": Decimal(".005"), "selection": Decimal("-.013"),
        "interaction": Decimal(".0005"), "total": Decimal("-.0075")}
    assert Decimal(report["reconciliationDifference"]) == 0


@pytest.mark.parametrize("change", [
    {"currency": "USD"}, {"portfolioReturn": ".04"}, {"benchmarkReturn": ".04"},
    {"basis": "ending_weights"}, {"sectors": []},
])
def test_incompatible_or_unreconciled_input_rejected(change):
    data = fixture(); data.update(change)
    with pytest.raises(ValueError):
        benchmark_attribution(data)


@pytest.mark.parametrize("key,value", [("portfolioWeight", "NaN"), ("portfolioReturn", True),
    ("benchmarkReturn", "-1.1"), ("portfolioWeight", "-.1"), ("benchmarkWeight", ".19"),
    ("portfolioReturn", "1e-999999"), ("portfolioReturn", None)])
def test_invalid_sector_input_rejected(key, value):
    data = fixture(); data["sectors"][0][key] = value
    with pytest.raises(ValueError):
        benchmark_attribution(data)


def test_duplicate_sector_rejected():
    data = fixture(); data["sectors"].append(deepcopy(data["sectors"][0]))
    with pytest.raises(ValueError):
        benchmark_attribution(data)
