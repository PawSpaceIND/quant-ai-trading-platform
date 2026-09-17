"""A container that is up is not a container that is working.

Three of the four pilot services shipped with `restart: unless-stopped` and no healthcheck,
which means the only failure the host could see was the process exiting. A Next.js server
whose event loop is wedged, or a quote collector whose poll loop has stopped returning,
keeps its container in the `running` state forever: `docker compose ps` reports it green,
the operator believes the pilot is live, and the dashboard quietly serves the last thing
anybody wrote.

The tests here are about what those checks are allowed to assert. A probe that greps for
its own process name would pass in exactly the situation it exists to catch, so each one
has to reach for the artifact the service is responsible for - the port the dashboard
serves, the snapshot file the collector rewrites - and the staleness bound has to sit
where a wedge trips it and a slow but working cycle does not.

The backup service is deliberately excluded, and that exclusion is pinned here too, so
that adding a plausible-looking check to a once-a-day cron loop stays a decision somebody
has to make on purpose.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy/docker-compose.yml"
COMPOSE = yaml.safe_load(COMPOSE_FILE.read_text())
SERVICES = COMPOSE["services"]

# The services that hold a socket or a loop open and can therefore wedge while alive.
SERVED_BY_A_HEALTHCHECK = {"pramana-ghost", "dashboard", "market-monitor", "token-watch"}
SNAPSHOT_PATH = "/data/market-monitor.json"


def probe(service: str) -> list[str]:
    return SERVICES[service]["healthcheck"]["test"]


def test_every_service_that_can_wedge_while_alive_declares_a_healthcheck():
    """`restart: unless-stopped` only reacts to an exit, so nothing else was watching.

    The engine has had a check since the pilot started; the dashboard and the collector
    are the two that could hang with the container still reported as running, and the
    engine's presence in the same set is what stops a refactor from dropping it.
    """
    declared = {name for name, service in SERVICES.items() if service.get("healthcheck")}
    assert declared == SERVED_BY_A_HEALTHCHECK


def test_the_backup_loop_is_left_without_a_check_rather_than_given_a_daily_one():
    """A 25-hour freshness bound would report `healthy` for a day after the loop died.

    `scripts/scheduled_backup.py` is cron in a `time.sleep` loop: it produces one artifact
    per `--interval-seconds`, 86400 by default. Any check over that artifact has to allow
    a full interval plus the 1800s backup subprocess timeout before it may complain, so
    the green light it shows is up to a day out of date - and `docker compose ps` presents
    it as a live answer. The reason is recorded next to the service in the compose file;
    this test exists so that a future check has to be argued for rather than pasted in.
    """
    assert "healthcheck" not in SERVICES["backup"]
    assert SERVICES["backup"]["entrypoint"] == ["python", "/app/scripts/scheduled_backup.py"]
    rationale = COMPOSE_FILE.read_text()
    assert "Deliberately has no healthcheck" in rationale


def test_the_dashboard_is_asked_for_a_real_response_on_the_port_it_publishes():
    """A probe pointed at a port nothing serves is a check that can only ever be red.

    The published mapping and the probe are two independent statements of the same port,
    written eighty lines apart, and nothing but this test ties them together. Accepting
    any response rather than a 200 would be the other half of the same mistake: Next.js
    answers 404 and 500 from a server that has stopped being useful.
    """
    published = SERVICES["dashboard"]["ports"][0]
    container_port = published.rsplit(":", 1)[-1]
    command = " ".join(probe("dashboard"))
    assert f"127.0.0.1:{container_port}/" in command
    assert "status===200" in command.replace(" ", "")


def test_the_collector_probe_rejects_a_snapshot_that_has_stopped_moving(tmp_path):
    """The real command, run against real files, because a string assertion proves nothing.

    `scripts/market_monitor.py` rewrites PRAMANA_MARKET_SNAPSHOT every cycle, and its
    `except` clause publishes an `unavailable` payload rather than dying - so a provider
    outage leaves a running process that keeps writing, and only a wedged process stops
    the file from moving. A probe that cannot tell a file written a minute ago from one
    written yesterday, or that treats a missing file as acceptable, would call the wedged
    case healthy.
    """
    command, bound = collector_probe()
    snapshot = tmp_path / "market-monitor.json"

    assert run_probe(command, snapshot) != 0, "a snapshot that was never written is not healthy"

    snapshot.write_text("{}")
    assert run_probe(command, snapshot) == 0

    os.utime(snapshot, (time.time() - bound + 120, time.time() - bound + 120))
    assert run_probe(command, snapshot) == 0, "a snapshot inside the bound is healthy"

    os.utime(snapshot, (time.time() - bound - 120, time.time() - bound - 120))
    assert run_probe(command, snapshot) != 0, "a snapshot past the bound is a wedged collector"


def test_the_collector_staleness_bound_sits_between_the_cadence_and_the_session():
    """Both ways of getting this number wrong are reachable by copying a real constant.

    Tightened to the dashboard's 120s `collectorStale` bound, the check goes red on every
    first cycle of the day, when the collector is downloading and parsing the broker's
    instrument master - the load this service was resized to 512m for. Loosened to a day,
    it reports a wedged collector as healthy through an entire session. The bound has to
    clear several of the loop's own `time.sleep` cadences and still fire well inside one
    09:15-15:30 session.
    """
    _, bound = collector_probe()
    monitor = (ROOT / "scripts/market_monitor.py").read_text()
    cadence = int(re.search(r"time\.sleep\((\d+)\)", monitor).group(1))
    session_seconds = int((15.5 - 9.25) * 3600)
    assert bound >= 5 * cadence
    assert bound <= session_seconds // 4


def test_the_capped_collector_gains_no_interpreter_from_being_watched():
    """512m is what it is because this service was OOM-killed at 256m, repeatedly.

    The comment above `mem_limit` records a measured 343 MB peak while the instrument
    master is parsed. A probe that starts a Python or Node runtime inside the same cgroup
    adds tens of megabytes to that peak every interval, and it would do so during exactly
    the cycle the start_period exists to protect - turning a monitoring addition into the
    OOM it was supposed to report.
    """
    assert SERVICES["market-monitor"]["mem_limit"] == "512m"
    command = " ".join(probe("market-monitor"))
    assert not re.search(r"\b(python|python3|node)\b", command)


def collector_probe() -> tuple[str, int]:
    """The collector's shell command and the staleness bound in seconds it encodes."""
    kind, command = probe("market-monitor")
    assert kind == "CMD-SHELL"
    bound = re.search(r"'(\d+) seconds ago'", command)
    assert bound, command
    return command, int(bound.group(1))


def run_probe(command: str, snapshot: Path) -> int:
    """Run the compose probe with its container path pointed at a temporary file."""
    assert SNAPSHOT_PATH in command
    return subprocess.run(
        ["sh", "-c", command.replace(SNAPSHOT_PATH, str(snapshot))],
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def test_the_shared_snapshot_path_is_the_one_the_probe_watches():
    """The probe hardcodes a container path; the services agree on it through `x-shared`."""
    assert SERVICES["market-monitor"]["environment"]["PRAMANA_MARKET_SNAPSHOT"] == SNAPSHOT_PATH
    assert SNAPSHOT_PATH in " ".join(probe("market-monitor"))


@pytest.mark.parametrize("service", sorted(SERVED_BY_A_HEALTHCHECK))
def test_a_check_is_given_room_to_start_and_a_timeout_shorter_than_its_interval(service):
    """A probe slower than its own interval stacks up; one with no start_period flaps.

    Docker runs the next probe `interval` after the previous one returns, so a timeout at
    or above the interval means a slow host is permanently mid-check. And every one of
    these services has a first-run cost - the engine opens the ledger, the dashboard boots
    Next.js, the collector downloads the instrument master - during which a failing probe
    is expected rather than interesting.
    """
    check = SERVICES[service]["healthcheck"]
    assert seconds(check.get("timeout", "0s")) < seconds(check.get("interval", "0s"))
    assert seconds(check.get("start_period", "0s")) >= seconds(check.get("interval", "0s"))
    assert check.get("retries", 0) >= 2


def seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)([sm])", value)
    assert match, value
    return int(match.group(1)) * (60 if match.group(2) == "m" else 1)


def test_token_watch_probe_rejects_missing_and_stale_heartbeat(tmp_path):
    kind, command = probe("token-watch")
    assert kind == "CMD-SHELL"
    path = tmp_path / "zerodha-watch.json"
    actual = command.replace("/data/zerodha-watch.json", str(path))
    def check():
        return subprocess.run(["sh", "-c", actual], capture_output=True, check=False).returncode
    assert check() != 0
    path.write_text("{}")
    assert check() == 0
    os.utime(path, (time.time() - 240, time.time() - 240))
    assert check() != 0
