"""Keep the open preflight requirements visible in the actual CI definition."""
import shlex
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def job():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["pilot-preflight-acceptance"]


def test_preflight_acceptance_is_explicit_and_failure_is_not_masked():
    configured = job()
    assert "if" not in configured
    assert not configured.get("continue-on-error", False)
    assert configured["env"]["TRADING_LIVE_MONEY_ACTIVE"] == "false"
    assert configured["env"]["PYTHONPATH"] == "src"
    step = next(item for item in configured["steps"] if item.get("name") == "Preflight input and report acceptance")
    assert "if" not in step and not step.get("continue-on-error", False)
    assert shlex.split(step["run"]) == [
        "python", "-m", "pytest", "-q", "acceptance/test_risk_preflight_input_boundary.py",
        "--junitxml=$RUNNER_TEMP/pilot-preflight-acceptance.xml",
    ]


def test_preflight_acceptance_retains_report_even_on_failure():
    step = next(item for item in job()["steps"] if item.get("uses", "").startswith("actions/upload-artifact@"))
    assert step["if"] == "always()"
    assert step["with"] == {"name": "pilot-preflight-acceptance",
                            "path": "${{ runner.temp }}/pilot-preflight-acceptance.xml",
                            "if-no-files-found": "error"}


def test_container_dashboard_calls_the_shared_risk_assertions():
    text = (ROOT / "tests/container_dashboard_smoke.mjs").read_text()
    active = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
    assert 'import { assertContainerFixtureRiskGates } from "./container_risk_gate_assertions.mjs";' in active
    assert "assertContainerFixtureRiskGates(gates);" in active
