"""Execute the actual Node smoke assertion with synthetic dashboard payloads."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fault", [None, "unarmed_sector", "armed_history", "missing_history",
                                  "wrong_records", "wrong_groups", "raised_limit",
                                  "wrong_schema", "too_few", "non_boolean", "invalid_setting",
                                  "unexpected_armed", "missing_tail"])
def test_offline_dashboard_fixture_assertion_is_exact(fault):
    schema = "pramana.risk_gates.v1"
    gates = [
        {"id": "sector_concentration", "armed": True, "records": 5, "groups": 3, "limit": .25},
        {"id": "correlation_adjusted_gross", "armed": False},
        {"id": "book_expected_shortfall", "armed": False},
        *({"id": f"other_{i}", "armed": False} for i in range(5)),
    ]
    for gate in gates:
        gate["setting"] = "PRAMANA_SYNTHETIC_SETTING"
    if fault == "unarmed_sector": gates[0]["armed"] = False
    elif fault == "armed_history": gates[1]["armed"] = True
    elif fault == "missing_history": gates.pop(1)
    elif fault == "wrong_records": gates[0]["records"] = 0
    elif fault == "wrong_groups": gates[0]["groups"] = 1
    elif fault == "raised_limit": gates[0]["limit"] = 1
    elif fault == "wrong_schema": schema = "wrong"
    elif fault == "too_few": gates = gates[:3]
    elif fault == "non_boolean": gates[3]["armed"] = 0
    elif fault == "invalid_setting": gates[3]["setting"] = "wrong"
    elif fault == "unexpected_armed": gates[3]["armed"] = True
    elif fault == "missing_tail": gates.pop(2)
    module = (ROOT / "tests/container_risk_gate_assertions.mjs").as_uri()
    script = (
        'import fs from "node:fs";\n'
        f'import {{ assertContainerFixtureRiskGates }} from {json.dumps(module)};\n'
        'assertContainerFixtureRiskGates(JSON.parse(fs.readFileSync(0, "utf8")));'
    )
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                            input=json.dumps({"schema": schema, "gates": gates}),
                            text=True, capture_output=True, timeout=20, check=False)
    if fault is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "AssertionError" in result.stderr
