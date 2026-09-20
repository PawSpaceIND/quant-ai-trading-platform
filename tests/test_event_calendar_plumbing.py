"""The event calendar reaches the container: compose mounts a host file and names it.

``event_calendar_from_env`` has read ``PRAMANA_EVENT_CALENDAR`` since the blackout work
landed, but compose never passed the variable or mounted a file, so no calendar the
operator wrote could reach the engine. The default mount is a committed example with no
events, which is exactly "no blackouts": the behaviour the pilot had before.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from quant_ai.governance.event_calendar import (
    EVENT_CALENDAR_ENV,
    event_calendar_from_env,
    event_calendar_from_json,
)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
EXAMPLE = DEPLOY / "event-calendar.example.json"
TARGET = "/app/event-calendar.json"


def ghost_service() -> dict:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["pramana-ghost"]


def test_compose_names_the_calendar_and_mounts_the_example_by_default():
    ghost = ghost_service()
    assert ghost["environment"][EVENT_CALENDAR_ENV] == TARGET
    mounts = {row["target"]: row for row in ghost["volumes"] if isinstance(row, dict)}
    mount = mounts[TARGET]
    assert mount["source"] == "${PRAMANA_EVENT_CALENDAR_HOST_FILE:-./event-calendar.example.json}"
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False


def test_the_example_calendar_validates_and_holds_no_events():
    calendar = event_calendar_from_env(environ={EVENT_CALENDAR_ENV: str(EXAMPLE)})
    assert calendar is not None
    assert calendar.timezone == "Asia/Kolkata"
    assert calendar.events == ()
    moment = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)
    assert calendar.blackout_reason("TRENT", moment) is None


def test_the_documented_format_in_env_example_is_the_one_the_loader_accepts():
    sample = {
        "timezone": "Asia/Kolkata",
        "events": [
            {"date": "2026-10-16", "category": "earnings", "symbol": "TRENT", "note": "Q2 results"},
            {"date": "2026-10-01", "category": "rbi_policy"},
            {"date": "2026-10-28", "through": "2026-10-29", "category": "fed_decision"},
        ],
    }
    calendar = event_calendar_from_json(sample)
    results_day = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)
    assert calendar.blackout_reason("TRENT", results_day) == "event_blackout:earnings"
    assert calendar.blackout_reason("BEL", results_day) is None
    policy_day = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)
    assert calendar.blackout_reason("BEL", policy_day) == "event_blackout:rbi_policy"
    assert calendar.blackout_reason("BEL", datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)) == "event_blackout:fed_decision"


def test_operator_calendar_file_is_ignored_so_the_deploy_tree_stays_clean():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "deploy/event-calendar.json" in ignored
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PRAMANA_EVENT_CALENDAR_HOST_FILE" in env_example
