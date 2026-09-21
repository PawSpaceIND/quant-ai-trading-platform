"""The overnight controls and the corporate-action calendar reach the engine container.

On 21 September 2026 the pre-market check reported ``overnight_exposure``,
``overnight_gap_monitor`` and ``corporate_actions`` unarmed. The first two are off by
default and armed by ``PRAMANA_OVERNIGHT_GROSS_CAP`` and ``PRAMANA_OVERNIGHT_GAP_MONITOR``,
but Compose never forwarded either to the engine, so a value in ``.env`` armed nothing; the
calendar had no mount at all. This pins the plumbing the same way the event calendar's is.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from quant_ai.marketdata.corporate_calendar import CorporateActionCalendar

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
EXAMPLE = DEPLOY / "corporate-actions.example.json"
TARGET = "/app/corporate-actions.json"


def ghost_service() -> dict:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["pramana-ghost"]


def test_compose_forwards_the_overnight_controls_with_their_documented_off_defaults():
    environment = ghost_service()["environment"]
    assert environment["PRAMANA_OVERNIGHT_GROSS_CAP"] == "${PRAMANA_OVERNIGHT_GROSS_CAP:-}"
    assert environment["PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES"] == "${PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES:-}"
    assert environment["PRAMANA_OVERNIGHT_GAP_MONITOR"] == "${PRAMANA_OVERNIGHT_GAP_MONITOR:-none}"


def test_compose_names_the_corporate_actions_file_and_mounts_the_example_by_default():
    ghost = ghost_service()
    assert ghost["environment"]["PRAMANA_CORPORATE_ACTIONS"] == TARGET
    mounts = {row["target"]: row for row in ghost["volumes"] if isinstance(row, dict)}
    mount = mounts[TARGET]
    assert mount["source"] == "${PRAMANA_CORPORATE_ACTIONS_HOST_FILE:-./corporate-actions.example.json}"
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False


def test_the_example_declares_the_schema_and_no_actions():
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert payload == {"schema": "pramana.corporate_actions.v1", "actions": []}
    calendar = CorporateActionCalendar.from_file(EXAMPLE)
    assert len(calendar) == 0
    assert calendar.action_on("TRENT", datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)) is None


def test_the_documented_format_in_env_example_is_the_one_the_loader_accepts(tmp_path):
    declared = tmp_path / "corporate-actions.json"
    declared.write_text(json.dumps({
        "schema": "pramana.corporate_actions.v1",
        "actions": [{"symbol": "INFY", "ex_date": "2026-10-14", "kind": "split_1_5"}],
    }))
    calendar = CorporateActionCalendar.from_file(declared)
    assert len(calendar) == 1
    assert calendar.action_on("INFY", datetime(2026, 10, 14, 4, 0, tzinfo=timezone.utc)) == "split_1_5"
    assert calendar.action_on("INFY", datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)) is None


def test_operator_declarations_file_is_ignored_so_the_deploy_tree_stays_clean():
    result = subprocess.run(
        ["git", "check-ignore", "-q", "deploy/corporate-actions.json"],
        cwd=ROOT, capture_output=True, check=False,
    )
    assert result.returncode == 0
