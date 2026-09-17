"""Native Bash/sed compatibility using throwaway Git and a fake Docker process only."""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest
from test_deploy_pilot_host import FAKE_SECRET, WEEKEND, advance_origin
from test_deploy_pilot_host import host as _host_fixture

# Expose the shared fixture without duplicating its local Git/Docker isolation.
host = _host_fixture

SERVICES = ("pramana-ghost", "dashboard", "market-monitor", "backup")
COUNTS = (41, 2, 9, 101)
CLOCK = f"{WEEKEND}T10:00:00+05:30"

FAKE_DOCKER = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["STUB_LOG"]).open("a") as log:
    log.write(" ".join(args) + "\n")
plan = json.loads(Path(os.environ["DEPLOY_TEST_PLAN"]).read_text())
if args[0] == "compose":
    if args[-2:] == ["config", "--services"]:
        print("\n".join(plan["services"]))
    elif args[-2:] == ["config", "--quiet"]:
        pass
    elif args[-3:] == ["up", "-d", "--build"]:
        pass
    elif "ps" in args:
        if args[-1] != plan.get("missing"):
            print("cid-" + args[-1])
    else:
        raise SystemExit(91)
elif args[0] == "inspect":
    service = args[-1].removeprefix("cid-")
    row = plan["rows"][service]
    seen = Path(os.environ["STUB_STATE"]) / args[-1]
    if seen.exists():
        print(row.get("status", "running"), row.get("restarting", "false"),
              row["after"], row.get("health", "healthy"))
    else:
        seen.touch()
        print("running", "false", row["before"], "starting")
else:
    raise SystemExit(92)
"""


def native_tools(site):
    """Use the actual system tools, not a newer shell that hides portability defects."""
    for name, executable in (("bash", "/bin/bash"), ("sed", "/usr/bin/sed")):
        assert Path(executable).is_file()
        script = site.base / "bin" / name
        script.write_text(f'#!/bin/sh\nexec {shlex.quote(executable)} "$@"\n')
        script.chmod(0o755)


def per_service_plan(site):
    native_tools(site)
    fake = site.base / "bin/docker"
    fake.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    fake.chmod(0o755)
    return {"services": list(SERVICES),
            "rows": {name: {"before": count, "after": count}
                     for name, count in zip(SERVICES, COUNTS, strict=True)}}


def run_plan(site, plan):
    path = site.base / "docker-plan.json"
    path.write_text(json.dumps(plan))
    return site.run(clock=CLOCK, DEPLOY_TEST_PLAN=str(path))


def test_native_tools_can_stamp_twice_without_backup_or_unrelated_byte_changes(host):
    native_tools(host)
    prefix = f"# synthetic environment\r\nZERODHA_ACCESS_TOKEN={FAKE_SECRET}\r\n".encode()
    prefix += b"UNRELATED='keep # this value'\r\n"
    host.env_file.write_bytes(prefix)
    ownership = (host.env_file.stat().st_uid, host.env_file.stat().st_gid)
    first = host.run(clock=CLOCK)
    assert first.returncode == 0, first.stderr
    next_head = advance_origin(host)
    second = host.run(clock=CLOCK)
    assert second.returncode == 0, second.stderr
    assert host.env_file.read_bytes() == prefix + f"PRAMANA_RELEASE_REVISION={next_head}\n".encode()
    assert host.env_file.stat().st_mode & 0o777 == 0o600
    assert (host.env_file.stat().st_uid, host.env_file.stat().st_gid) == ownership
    assert list(host.root.glob(".env*")) == [host.env_file]
    assert FAKE_SECRET not in first.stdout + first.stderr + second.stdout + second.stderr


def test_each_service_retains_its_own_unchanged_restart_baseline(host):
    plan = per_service_plan(host)
    result = run_plan(host, plan)
    assert result.returncode == 0, result.stderr
    inspected = [call.rsplit(" ", 1)[-1] for call in host.docker_calls if call.startswith("inspect ")]
    assert inspected == [f"cid-{name}" for name in SERVICES] * 2
    for name, count in zip(SERVICES, COUNTS, strict=True):
        assert f"deploy: {name} status=running restarts={count} health=healthy" in result.stdout
    assert "all 4 services running" in result.stdout


@pytest.mark.parametrize("service", SERVICES)
def test_restart_in_each_service_is_compared_to_that_same_service(host, service):
    plan = per_service_plan(host)
    initial = plan["rows"][service]["before"]
    plan["rows"][service]["after"] = initial + 1
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert f"deploy: {service} restarted {initial}->{initial + 1}" in result.stderr
    assert "1 container check(s) failed" in result.stderr
    assert "all 4 services running" not in result.stdout


@pytest.mark.parametrize("service", (SERVICES[0], SERVICES[-1]))
def test_health_of_first_and_last_service_is_not_dropped_by_indexing(host, service):
    plan = per_service_plan(host)
    plan["rows"][service]["health"] = "unhealthy"
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert f"deploy: {service} is unhealthy" in result.stderr
    assert "1 container check(s) failed" in result.stderr


def test_missing_middle_container_refuses_before_inspection(host):
    plan = per_service_plan(host)
    plan["missing"] = SERVICES[2]
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert f"service '{SERVICES[2]}' has no container" in result.stderr
    assert not any(call.startswith("inspect ") for call in host.docker_calls)


def test_empty_service_inventory_never_reports_success(host):
    plan = per_service_plan(host)
    plan["services"] = []
    result = run_plan(host, plan)
    assert result.returncode != 0
    assert "declares no services" in result.stderr
    assert not any(call.startswith("inspect ") for call in host.docker_calls)


def test_non_main_branch_remains_refused_before_any_docker_call(host):
    native_tools(host)
    host.git("checkout", "-b", "feature/fixture-only")
    original = host.env_file.read_bytes()
    result = host.run(clock=CLOCK)
    assert result.returncode != 0 and "deploy from main" in result.stderr
    assert host.docker_calls == [] and host.env_file.read_bytes() == original
