"""Docker evidence failures must never become successful deployment reports.

All commands use the existing disposable Git host and synthetic Docker executable.
"""
from __future__ import annotations

import pytest
from test_deploy_pilot_host import host as _host_fixture
from test_deploy_pilot_portability import per_service_plan, run_plan

host = _host_fixture
MARKER = "PRIVATE_DIAGNOSTIC_MUST_NOT_APPEAR"


def evidence_plan(site):
    plan = per_service_plan(site)
    fake = site.base / "bin/docker"
    source = fake.read_text()
    source = source.replace('if args[0] == "compose":', '''if args[0] == "compose":
    if args[-2:] == ["config", "--services"] and "services_exit" in plan:
        print("\\n".join(plan["services"]))
        raise SystemExit(plan["services_exit"])
    if "ps" in args and "ps_exit" in plan:
        print("cid-" + args[-1])
        raise SystemExit(plan["ps_exit"])
    if "ps" in args and args[-1] == plan.get("extra_container"):
        print("cid-" + args[-1] + "\\ncid-unchecked")
        raise SystemExit(0)''')
    source = source.replace('    if seen.exists():', '''    phase = "after" if seen.exists() else "before"
    if "override_" + phase in row:
        seen.touch()
        print(row["override_" + phase])
        raise SystemExit(row.get("exit_" + phase, 0))
    if seen.exists():''')
    fake.write_text(source)
    return plan


@pytest.mark.parametrize("phase", ["before", "after"])
def test_failed_inspection_cannot_pass_even_with_valid_stdout(host, phase):
    plan = evidence_plan(host)
    plan["rows"]["dashboard"].update({"override_" + phase: "running false 2 healthy",
                                      "exit_" + phase: 42})
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "all 4 services running" not in result.stdout


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("payload", [
    "running false not-a-number healthy", "running false -1 healthy",
    "running false 9223372036854775808 healthy", "running perhaps 2 healthy",
    "running false 2 unknown", "running false 2",
    "running false 2 healthy " + MARKER,
    "running false 2 healthy\nrunning false 2 healthy",
])
def test_malformed_inspection_refuses_without_echoing_payload(host, phase, payload):
    plan = evidence_plan(host)
    plan["rows"]["dashboard"]["override_" + phase] = payload
    result = run_plan(host, plan)
    assert result.returncode != 0
    expected = "invalid restart count" if "9223372036854775808" in payload else "invalid inspection"
    assert expected in result.stderr
    assert "all 4 services running" not in result.stdout
    assert MARKER not in result.stdout + result.stderr


def test_failed_service_inventory_cannot_certify_partial_output(host):
    plan = evidence_plan(host)
    plan["services_exit"] = 42
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "all 4 services running" not in result.stdout


def test_multiple_containers_cannot_certify_only_the_first(host):
    plan = evidence_plan(host)
    plan["extra_container"] = "dashboard"
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "ambiguous container identity" in result.stderr
    assert not any(call.startswith("inspect ") for call in host.docker_calls)
    assert "all 4 services running" not in result.stdout


def test_restart_counter_decrease_does_not_certify_continuity(host):
    plan = evidence_plan(host)
    plan["rows"]["dashboard"].update(before=2, after=1)
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "all 4 services running" not in result.stdout


def test_failed_container_lookup_cannot_use_its_stdout(host):
    plan = evidence_plan(host)
    plan["ps_exit"] = 42
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "services running" not in result.stdout


@pytest.mark.parametrize("name", ["dashboard", "bad name " + MARKER])
def test_duplicate_or_malformed_service_list_is_refused(host, name):
    plan = evidence_plan(host)
    plan["services"].append(name)
    plan["rows"][name] = {"before": 2, "after": 2}
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "services running" not in result.stdout
    assert MARKER not in result.stdout + result.stderr


def test_maximum_supported_restart_count_remains_valid(host):
    plan = evidence_plan(host)
    plan["rows"]["dashboard"].update(before=9223372036854775807, after=9223372036854775807)
    result = run_plan(host, plan)
    assert result.returncode == 0, result.stderr
    assert "all 4 services running" in result.stdout
