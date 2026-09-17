"""Throwaway Git remotes and Docker stubs only; never deploy the user's services."""
from __future__ import annotations

import os

import pytest
from test_deploy_pilot_host import FAKE_SECRET, WEEKEND
from test_deploy_pilot_host import host as host_fixture

CLOCK = f"{WEEKEND}T10:00:00+05:30"


@pytest.fixture
def host(tmp_path):
    return host_fixture.__wrapped__(tmp_path)


def assert_no_secret(result):
    assert FAKE_SECRET not in result.stdout + result.stderr


def change_docker(host, old, new):
    stub = host.base / "bin/docker"
    source = stub.read_text()
    assert old in source
    stub.write_text(source.replace(old, new, 1))


def test_system_bash_runs_all_container_checks_and_repeated_stamp(host):
    # macOS exercises its real Bash 3.2; Linux exercises /bin/bash in CI.
    (host.base / "bin/bash").symlink_to("/bin/bash")
    for _ in range(2):
        result = host.run(clock=CLOCK)
        assert result.returncode == 0, result.stderr
        assert "all 4 services running" in result.stdout
        assert_no_secret(result)
    assert len([x for x in host.docker_calls if x.startswith("inspect ")]) == 16


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_revision_rewrite_preserves_unrelated_bytes_and_private_metadata(host, ending):
    prefix = f"# synthetic credential only\r\nZERODHA_ACCESS_TOKEN={FAKE_SECRET}".encode() + b"\r\n"
    tail = b"OTHER='spaces = dollar $ and unicode \xc3\xa9'" + ending
    host.env_file.write_bytes(prefix + b"PRAMANA_RELEASE_REVISION=old\r\n" + tail)
    before = host.env_file.stat()
    result = host.run(clock=CLOCK)
    assert result.returncode == 0, result.stderr
    after = host.env_file.stat()
    assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid)
    data = host.env_file.read_bytes()
    assert data.startswith(prefix)
    assert tail in data
    assert data.count(b"PRAMANA_RELEASE_REVISION=") == 1
    assert f"PRAMANA_RELEASE_REVISION={host.head}".encode() in data
    assert_no_secret(result)


def test_missing_final_newline_does_not_join_stamp_to_credential(host):
    original = f"ZERODHA_ACCESS_TOKEN={FAKE_SECRET}".encode()
    host.env_file.write_bytes(original)
    result = host.run(clock=CLOCK)
    assert result.returncode == 0, result.stderr
    assert host.env_file.read_bytes() == original + b"\n" + f"PRAMANA_RELEASE_REVISION={host.head}\n".encode()
    assert_no_secret(result)


def test_duplicate_revision_lines_are_replaced_by_one_unambiguous_stamp(host):
    host.env_file.write_text(f"ZERODHA_ACCESS_TOKEN={FAKE_SECRET}\nPRAMANA_RELEASE_REVISION=old\nPRAMANA_RELEASE_REVISION=other\n")
    result = host.run(clock=CLOCK)
    assert result.returncode == 0, result.stderr
    lines = host.env_file.read_text().splitlines()
    assert [x for x in lines if x.startswith("PRAMANA_RELEASE_REVISION=")] == [f"PRAMANA_RELEASE_REVISION={host.head}"]


@pytest.mark.parametrize("value", ["", "-1", "1.5", "abc", "99999999999999999999"])
def test_invalid_settle_interval_refuses_before_checkout_or_docker(host, value):
    before, head = host.env_file.read_bytes(), host.head
    result = host.run(clock=CLOCK, PRAMANA_DEPLOY_SETTLE_SECONDS=value)
    assert result.returncode != 0
    assert "settle" in result.stderr.lower()
    assert host.env_file.read_bytes() == before and host.head == head
    assert host.docker_calls == []


@pytest.mark.parametrize("defect", ["command_error", "missing", "malformed", "negative", "overflow", "extra_field"])
def test_untrustworthy_container_inspection_never_reports_deployment_success(host, defect):
    injected = {
        "command_error": 'exit 73',
        "missing": 'exit 0',
        "malformed": 'echo "running false unknown healthy"; exit 0',
        "negative": 'echo "running false -1 healthy"; exit 0',
        "overflow": 'echo "running false 99999999999999999999999 healthy"; exit 0',
        "extra_field": 'echo "running false 0 healthy extra"; exit 0',
    }[defect]
    change_docker(host, 'if [ "$1" = "inspect" ]; then', 'if [ "$1" = "inspect" ]; then\n  ' + injected)
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert "inspect" in result.stderr.lower()
    assert "all 4 services running" not in result.stdout
    assert_no_secret(result)


@pytest.mark.parametrize("change", [0, 1])
def test_restart_counters_stay_bound_to_the_correct_service(host, change):
    setup = f'''if [ "$1" = "inspect" ]; then
  case "$last" in
    cid-pramana-ghost) STUB_RESTARTS_BEFORE=12; STUB_RESTARTS_AFTER=12;;
    cid-dashboard) STUB_RESTARTS_BEFORE=3; STUB_RESTARTS_AFTER={3 + change};;
    *) STUB_RESTARTS_BEFORE=0; STUB_RESTARTS_AFTER=0;;
  esac'''
    change_docker(host, 'if [ "$1" = "inspect" ]; then', setup)
    result = host.run(clock=CLOCK)
    assert (result.returncode == 0) is (change == 0), result.stderr
    if change:
        assert "dashboard restarted 3->4" in result.stderr
        assert "pramana-ghost restarted" not in result.stderr
    assert_no_secret(result)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_env_file_alias_is_refused_without_changing_credential_target(host, kind):
    target = host.base / "private-env"
    target.write_bytes(host.env_file.read_bytes())
    target.chmod(0o600)
    host.env_file.unlink()
    if kind == "symlink":
        host.env_file.symlink_to(target)
    else:
        os.link(target, host.env_file)
    before = target.read_bytes()
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert target.read_bytes() == before
    assert not any("up -d --build" in x for x in host.docker_calls)
    assert_no_secret(result)


@pytest.mark.parametrize("reply", ["exit 37", "exit 0", 'printf "dashboard\\ndashboard\\n"; exit 0', 'echo "invalid service"; exit 0'])
def test_service_enumeration_failure_refuses_before_build(host, reply):
    replacement = 'if [ "$1" = "compose" ]; then\n  case " $* " in *" config --services "*) ' + reply + ';; esac'
    change_docker(host, 'if [ "$1" = "compose" ]; then', replacement)
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert not any("up -d --build" in x for x in host.docker_calls)
    assert_no_secret(result)


def test_multiple_containers_are_not_silently_reduced_to_the_first(host):
    replacement = 'if [ "$1" = "compose" ]; then\n  case " $* " in *" ps --all --quiet "*) printf "one\\ntwo\\n"; exit 0;; esac'
    change_docker(host, 'if [ "$1" = "compose" ]; then', replacement)
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert "exactly one container" in result.stderr
    assert "all 4 services running" not in result.stdout


def test_publicly_readable_environment_is_not_rewritten_or_deployed(host):
    host.env_file.chmod(0o644)
    before = host.env_file.read_bytes()
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert host.env_file.read_bytes() == before
    assert not any("up -d --build" in x for x in host.docker_calls)
    assert_no_secret(result)


@pytest.mark.parametrize("failure", ["exception", "process_exit"])
def test_interrupted_stamp_preserves_original_credentials_and_never_builds(host, failure):
    script = host.root / "scripts/deploy_pilot_host.sh"
    source = script.read_text()
    needle = "        os.replace(temporary, path)"
    assert needle in source
    fault = 'raise OSError("synthetic rename failure")' if failure == "exception" else "os._exit(79)"
    script.write_text(source.replace(needle, "        " + fault + "\n" + needle, 1))
    host.git("add", "scripts/deploy_pilot_host.sh")
    host.git("commit", "-m", "synthetic stamp interruption")
    host.git("push", "origin", "main")
    before = host.env_file.read_bytes()
    result = host.run(clock=CLOCK)
    assert result.returncode != 0
    assert host.env_file.read_bytes() == before
    assert host.env_file.stat().st_mode & 0o777 == 0o600
    assert not any("up -d --build" in x for x in host.docker_calls)
    assert_no_secret(result)
    leftovers = list(host.root.glob(".env.release-*"))
    if failure == "exception":
        assert leftovers == []
    else:
        assert len(leftovers) == 1
        assert leftovers[0].stat().st_mode & 0o777 == 0o600
        retry = host.run(clock=CLOCK)
        assert retry.returncode != 0 and "dirty" in retry.stderr
        assert host.env_file.read_bytes() == before


def test_decreasing_restart_counter_is_not_accepted_as_a_healthy_observation(host):
    result = host.run(clock=CLOCK, STUB_RESTARTS_BEFORE=5, STUB_RESTARTS_AFTER=4)
    assert result.returncode != 0
    assert "restart counter decreased" in result.stderr
    assert "all 4 services running" not in result.stdout


@pytest.mark.parametrize("stub", [{"STUB_RESTARTING": "unknown"}, {"STUB_HEALTH": "unknown"}])
def test_invalid_second_observation_fails_instead_of_assuming_health(host, stub):
    result = host.run(clock=CLOCK, **stub)
    assert result.returncode != 0
    assert "container inspection" in result.stderr
    assert "all 4 services running" not in result.stdout
