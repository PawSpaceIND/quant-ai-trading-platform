from copy import deepcopy
from decimal import Decimal

import pytest

from quant_ai.analytics.benchmark_attribution import benchmark_attribution


def fixture():
    return {"schema": "pramana.benchmark_attribution_input.v1", "currency": "INR",
            "portfolioId": "example-portfolio", "benchmarkId": "example-benchmark",
            "periodStart": "2026-08-01", "periodEnd": "2026-09-01",
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


def test_private_cli_binds_exact_input_and_refuses_overwrite(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys
    from pathlib import Path

    source, destination = tmp_path / "input.json", tmp_path / "report.json"
    source.write_text(json.dumps(fixture()))
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/benchmark_attribution.py"),
               "--input", str(source), "--output", str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(destination.read_text())
    assert report["inputSha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["input"] == fixture()
    assert destination.stat().st_mode & 0o777 == 0o600
    original = destination.read_bytes()
    assert subprocess.run(command, capture_output=True, check=False).returncode == 2
    assert destination.read_bytes() == original
    source.write_text('{}')
    command[-1] = str(tmp_path / "invalid.json")
    assert subprocess.run(command, capture_output=True, check=False).returncode == 2
    assert not Path(command[-1]).exists()


@pytest.mark.parametrize("key,value", [("portfolioId", ""), ("benchmarkId", None),
    ("periodStart", "2026-09-01"), ("periodStart", "2026-09-02"),
    ("periodStart", "2026-02-30"), ("periodEnd", "20260901"), ("benchmarkId", "bad\nname")])
def test_missing_or_invalid_period_identity_rejected(key, value):
    data = fixture(); data[key] = value
    with pytest.raises(ValueError):
        benchmark_attribution(data)


def test_report_retains_identity_and_does_not_claim_source_qualification():
    data = fixture(); result = benchmark_attribution(data)
    for key in ("portfolioId", "benchmarkId", "periodStart", "periodEnd"):
        assert result[key] == data[key]
    assert result["sourceQualified"] is False
