"""The deploy that must refuse more often than it runs.

Deploying the pilot host is `up -d --build`, which replaces the engine container. The
market-data candle aggregator inside it is in memory only, so a rebuild during a session
throws away every intraday candle built since the open and the technical agent has nothing
to decide on for roughly fifty minutes - while the paper book stays open. The hand-typed
sequence in docs/VPS_GHOST_DEPLOYMENT.md asked the operator to remember that, along with
pulling first, stamping the revision that was actually built, and looking at whether the
containers came back.

The tests here drive scripts/deploy_pilot_host.sh end to end against a throwaway git
remote and a stubbed `docker`, and they are mostly about the refusals. The session gate is
exercised across a trading hour, a weekend and an NSE holiday, because a gate written as
"weekday between 09:15 and 15:30" passes the weekend case and blocks a perfectly good
holiday deploy - the calendar in src/quant_ai/execution/session.py is the only authority
that gets all three right. The rest pin the things a deploy can quietly get wrong: a
revision stamped from a dirty tree describes an image nobody can reproduce, and a
container in a crash loop is `running` again by the time anyone looks at it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy_pilot_host.sh"

# 2026-09-15 is a Tuesday, 2026-09-12 the Saturday before it, and 2026-09-14 the Monday
# between them, which NSE_HOLIDAYS_2026 closes. None of that is restated in the script.
TRADING_TUESDAY = "2026-09-15"
WEEKEND = "2026-09-12"
NSE_HOLIDAY = "2026-09-14"

# A value shaped like the broker token the host keeps in .env, so the "never print a
# secret" test has something recognisable to look for.
FAKE_SECRET = "zerodha-access-token-NEVER-PRINT-THIS"

DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$STUB_LOG"
if [ "$1" = "compose" ]; then
  case " $* " in
    *" config --services "*) printf '%s\\n' pramana-ghost dashboard market-monitor backup;;
    *" config --quiet "*) exit "${STUB_CONFIG_EXIT:-0}";;
    *" up -d --build "*) echo "stub: rebuilt"; exit "${STUB_UP_EXIT:-0}";;
    *" ps --all --quiet "*) echo "cid-${!#}";;
  esac
  exit 0
fi
if [ "$1" = "inspect" ]; then
  seen="$STUB_STATE/${!#}"
  if [ -e "$seen" ]; then
    echo "${STUB_STATUS:-running} ${STUB_RESTARTING:-false} ${STUB_RESTARTS_AFTER:-0} ${STUB_HEALTH:-healthy}"
  else
    : >"$seen"
    echo "running false ${STUB_RESTARTS_BEFORE:-0} starting"
  fi
  exit 0
fi
exit 0
"""


class Host:
    """A throwaway checkout of the pilot host, with its own origin and a stubbed docker."""

    def __init__(self, base: Path):
        self.base = base
        self.origin = base / "origin.git"
        self.root = base / "host"
        self.env_file = self.root / ".env"
        self.docker_log = base / "docker.log"
        self.stub_state = base / "stub-state"
        self.stub_state.mkdir()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **GIT_IDENTITY},
        ).stdout.strip()

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def run(self, *args: str, clock: str | None = None, **stub) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            **GIT_IDENTITY,
            "PATH": f"{self.base / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PRAMANA_DEPLOY_SETTLE_SECONDS": "0",
            "STUB_LOG": str(self.docker_log),
            "STUB_STATE": str(self.stub_state),
            **{key: str(value) for key, value in stub.items()},
        }
        if clock:
            environment["PRAMANA_DEPLOY_CLOCK"] = clock
        return subprocess.run(
            ["bash", str(self.root / "scripts/deploy_pilot_host.sh"), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    @property
    def docker_calls(self) -> list[str]:
        return self.docker_log.read_text().splitlines() if self.docker_log.exists() else []


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Pilot Test",
    "GIT_AUTHOR_EMAIL": "pilot@example.invalid",
    "GIT_COMMITTER_NAME": "Pilot Test",
    "GIT_COMMITTER_EMAIL": "pilot@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


@pytest.fixture
def host(tmp_path: Path) -> Host:
    site = Host(tmp_path)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(site.origin)], check=True,
                   capture_output=True)
    site.root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(site.root)], check=True, capture_output=True)
    (site.root / "scripts").mkdir()
    (site.root / "deploy").mkdir()
    shutil.copy2(SCRIPT, site.root / "scripts/deploy_pilot_host.sh")
    shutil.copy2(ROOT / "deploy/docker-compose.yml", site.root / "deploy/docker-compose.yml")
    shutil.copy2(ROOT / "pyproject.toml", site.root / "pyproject.toml")
    # The gate imports the real calendar through the checkout's own src/, exactly as it
    # does on the host. A symlink keeps the fixture cheap without faking the module.
    (site.root / "src").symlink_to(ROOT / "src")
    (site.root / ".gitignore").write_text(".env\n")
    site.git("add", "-A")
    site.git("commit", "-m", "pilot host fixture")
    site.git("remote", "add", "origin", str(site.origin))
    site.git("push", "-u", "origin", "main")
    site.env_file.write_text(f"ZERODHA_ACCESS_TOKEN={FAKE_SECRET}\n")
    site.env_file.chmod(0o600)
    (tmp_path / "bin").mkdir()
    stub = tmp_path / "bin/docker"
    stub.write_text(DOCKER_STUB)
    stub.chmod(0o755)
    return site


def advance_origin(host: Host) -> str:
    """Commit on origin so the deploy has something to fast-forward to."""
    clone = host.base / "author"
    subprocess.run(["git", "clone", str(host.origin), str(clone)], check=True, capture_output=True)
    (clone / "RELEASE").write_text("next\n")
    host.git("add", "-A", cwd=clone)
    host.git("commit", "-m", "next release", cwd=clone)
    host.git("push", "origin", "main", cwd=clone)
    return host.git("rev-parse", "HEAD", cwd=clone)


def test_a_deploy_started_from_anywhere_but_the_repository_root_stops_before_docker(host):
    """Relative paths and a compose build context make the wrong directory a silent bug.

    Every path in the script is relative and `--build` resolves its context from the
    compose file, so running from `scripts/` would stamp one .env, build from another
    directory and pull a third. The refusal has to come before anything is pulled or
    rebuilt, which is why the assertion is on the docker log being untouched.
    """
    result = subprocess.run(
        ["bash", str(host.root / "scripts/deploy_pilot_host.sh")],
        cwd=host.root / "scripts",
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **GIT_IDENTITY, "PATH": f"{host.base / 'bin'}{os.pathsep}{os.environ['PATH']}",
             "STUB_LOG": str(host.docker_log), "STUB_STATE": str(host.stub_state),
             "PRAMANA_DEPLOY_CLOCK": f"{WEEKEND}T10:00:00+05:30"},
    )
    assert result.returncode != 0
    assert "repository root" in result.stderr
    assert host.docker_calls == []


def test_regular_hours_refuse_the_rebuild_that_would_empty_the_candle_aggregator(host):
    """The whole reason this script exists rather than four lines of runbook.

    The engine's candle aggregator is in memory, so `up -d --build` at 10:00 costs the
    technical agent its entire decision basis for the next fifty minutes while positions
    stay open. Refusing has to happen before the pull as well as before the build: a
    checkout moved forward and then abandoned leaves the host on source nobody deployed.
    """
    before = host.head
    result = host.run(clock=f"{TRADING_TUESDAY}T10:00:00+05:30")
    assert result.returncode != 0
    assert "regular hours" in result.stderr
    assert "09:15-15:30" in result.stderr
    assert host.docker_calls == []
    assert host.head == before


def test_force_accepts_the_cost_out_loud_and_deploys_anyway(host):
    """An override that is silent is an override nobody remembers using."""
    result = host.run("--force", clock=f"{TRADING_TUESDAY}T10:00:00+05:30")
    assert result.returncode == 0, result.stderr
    assert "candle aggregator will rebuild from empty" in result.stdout
    assert any("up -d --build" in call for call in host.docker_calls)


@pytest.mark.parametrize(
    "clock,allowed",
    [
        (f"{TRADING_TUESDAY}T09:10:00+05:30", True),   # pre-open
        (f"{TRADING_TUESDAY}T09:20:00+05:30", False),  # five minutes after the open
        (f"{TRADING_TUESDAY}T15:29:00+05:30", False),  # one minute before the close
        (f"{TRADING_TUESDAY}T15:45:00+05:30", True),   # post-market
        (f"{WEEKEND}T10:00:00+05:30", True),           # Saturday
        (f"{NSE_HOLIDAY}T10:00:00+05:30", True),       # a Monday NSE is closed
    ],
)
def test_the_gate_asks_the_trading_calendar_and_not_the_clock_face(host, clock, allowed):
    """A hand-written "weekday, 09:15 to 15:30" passes five of these six cases.

    The one it fails is the holiday: it would block a deploy on a Monday the exchange is
    shut, which is one of the few days the operator can rebuild freely. Reading
    MarketCalendar means the holiday list, the Budget-Sunday special session and the
    weekend all come from the file the engine itself judges by, and the boundary minutes
    come from SESSIONS rather than from this script's memory of them.
    """
    result = host.run(clock=clock)
    assert (result.returncode == 0) is allowed, result.stderr


def test_the_stamped_revision_is_the_head_that_was_just_pulled(host):
    """The dashboard and the strategy manifest compare against this value.

    PRAMANA_RELEASE_REVISION is a deployment declaration, so a stamp left at the previous
    release makes the review gate attest to source that is not running. Stamping has to
    happen after the pull and from the new HEAD, and it has to leave the rest of .env -
    live broker credentials - untouched.
    """
    pushed = advance_origin(host)
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30")
    assert result.returncode == 0, result.stderr
    assert host.head == pushed
    assert f"PRAMANA_RELEASE_REVISION={pushed}\n" in host.env_file.read_text()
    assert f"ZERODHA_ACCESS_TOKEN={FAKE_SECRET}\n" in host.env_file.read_text()
    assert pushed in result.stdout


def test_a_second_deploy_replaces_the_stamp_instead_of_appending_another(host):
    """Two PRAMANA_RELEASE_REVISION lines mean .env says whatever the parser reads last."""
    host.run(clock=f"{WEEKEND}T10:00:00+05:30")
    pushed = advance_origin(host)
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30")
    assert result.returncode == 0, result.stderr
    stamps = [line for line in host.env_file.read_text().splitlines()
              if line.startswith("PRAMANA_RELEASE_REVISION=")]
    assert stamps == [f"PRAMANA_RELEASE_REVISION={pushed}"]


def test_a_dirty_checkout_refuses_rather_than_stamping_a_revision_it_cannot_describe(host):
    """An uncommitted edit builds an image that no commit reproduces.

    The stamp would still be a valid 40-character SHA, and everything downstream would
    treat it as the truth, which is what makes this failure quiet. Refusing before the
    pull also avoids `--ff-only` failing halfway through for a less obvious reason.
    """
    (host.root / "deploy/docker-compose.yml").write_text("# edited on the host\n")
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30")
    assert result.returncode != 0
    assert "dirty" in result.stderr
    assert host.docker_calls == []
    assert "PRAMANA_RELEASE_REVISION" not in host.env_file.read_text()


def test_a_container_that_keeps_restarting_fails_the_deploy(host):
    """A crash loop is `running` again a second after you look at it.

    `restart: unless-stopped` puts a container that exits on startup straight back into
    the running state, so a single `docker compose ps` after the build reports the whole
    stack green. The restart counter moving during the settle window is the only thing
    that separates a crash loop from a clean start, and the deploy has to exit non-zero
    on it rather than print a line the operator scrolls past.
    """
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30",
                      STUB_RESTARTS_BEFORE=0, STUB_RESTARTS_AFTER=4)
    assert result.returncode != 0
    assert "restart loop" in result.stderr
    assert "container check(s) failed" in result.stderr


@pytest.mark.parametrize(
    "stub,expected",
    [
        ({"STUB_STATUS": "exited"}, "not running"),
        ({"STUB_RESTARTING": "true"}, "mid-restart"),
        ({"STUB_HEALTH": "unhealthy"}, "unhealthy"),
    ],
)
def test_a_service_that_did_not_come_back_fails_the_deploy(host, stub, expected):
    """Three ways to be down that all leave `up -d --build` exiting zero.

    Compose reports success once it has issued the start; what the containers did next is
    a separate question, and an unhealthy one is now answerable because the checks added
    alongside this script exist.
    """
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30", **stub)
    assert result.returncode != 0
    assert expected in result.stderr


def test_a_healthy_deploy_reports_every_service_and_the_revision(host):
    """`starting` is not a failure: the collector's check waits on its first snapshot."""
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30", STUB_HEALTH="starting")
    assert result.returncode == 0, result.stderr
    for service in ("pramana-ghost", "dashboard", "market-monitor", "backup"):
        assert f"deploy: {service} status=running" in result.stdout
    assert f"all 4 services running at revision {host.head}" in result.stdout


def test_an_invalid_compose_configuration_stops_before_anything_is_rebuilt(host):
    """A missing required variable in .env must not take the running stack down with it."""
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30", STUB_CONFIG_EXIT=1)
    assert result.returncode != 0
    assert "nothing was deployed" in result.stderr
    assert not any("up -d --build" in call for call in host.docker_calls)


def test_nothing_the_deploy_prints_carries_a_value_out_of_env(host):
    """.env on the pilot host holds live broker and model credentials.

    The one command here that could print them is `compose config`, which writes the fully
    rendered configuration unless it is asked not to, and a `set -x` anywhere in the file
    would put every expanded command line into the operator's scrollback.
    """
    result = host.run(clock=f"{WEEKEND}T10:00:00+05:30")
    assert result.returncode == 0, result.stderr
    assert FAKE_SECRET not in result.stdout
    assert FAKE_SECRET not in result.stderr
    # `config --services` prints service names and nothing else; a bare `config` renders
    # every interpolated value, so no call is allowed to leave out one of the two.
    rendering = [call for call in host.docker_calls if " config" in call]
    assert rendering
    assert all("--quiet" in call or "--services" in call for call in rendering)
    source = SCRIPT.read_text()
    assert "set -euo pipefail" in source
    executable = [line.strip() for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in executable if line.startswith(("set -x", "set +e"))]
