"""CI fixture mount contract; no container, network, broker or risk-rule mutation."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("container_verifier", ROOT / "scripts/verify_pilot_containers.py")
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)
SETTING = "PRAMANA_SECTOR_MAP_FILE"


def config(tmp_path):
    source = tmp_path / "operator-map.json"
    source.write_text('{"INFY":"IT_SERVICES"}')
    return source, {"environment": {SETTING: "/app/pilot-sector-map.json"}, "volumes": [
        {"type": "bind", "source": str(source), "target": "/app/pilot-sector-map.json", "read_only": True}]}


def test_mount_binds_the_file_declared_by_the_actual_service(tmp_path):
    source, service = config(tmp_path)
    original = source.read_bytes()
    assert verifier.read_only_environment_file_mount(service, SETTING) == [
        "--mount", f"type=bind,src={source},dst=/app/pilot-sector-map.json,readonly"]
    assert source.read_bytes() == original


@pytest.mark.parametrize("fault", ["missing", "duplicate", "writable", "volume", "wrong_target"])
def test_incomplete_or_unsafe_fixture_mount_refuses(tmp_path, fault):
    _, service = config(tmp_path)
    if fault == "missing": service["volumes"] = []
    elif fault == "duplicate": service["volumes"] *= 2
    elif fault == "writable": service["volumes"][0]["read_only"] = False
    elif fault == "volume": service["volumes"][0]["type"] = "volume"
    else: service["volumes"][0]["target"] = "/other.json"
    with pytest.raises(ValueError, match="file_mount_invalid"):
        verifier.read_only_environment_file_mount(service, SETTING)


@pytest.mark.parametrize("fault", ["absent", "directory", "relative"])
def test_a_rendered_path_is_not_proof_the_bind_source_exists(tmp_path, fault):
    source, service = config(tmp_path)
    if fault == "absent": source.unlink()
    elif fault == "directory": source.unlink(); source.mkdir()
    else: service["volumes"][0]["source"] = "relative.json"
    with pytest.raises(ValueError, match="file_source_missing"):
        verifier.read_only_environment_file_mount(service, SETTING)
