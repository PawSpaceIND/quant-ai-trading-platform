"""An isolated probe may spend once but cannot invoke trading execution."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.provenance import content_hash

SPEC = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "scripts/probe_consensus_response.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def response(stop="tool_use"):
    body = {"stance": "NEUTRAL", "confidence": 0.0, "expected_return": 0.0,
        "expected_risk": 0.0, "rationale": ["PRIVATE_RESPONSE_TEXT"],
        "xai_proof": {"summary": "PRIVATE_RESPONSE_TEXT", "supporting_factors": [], "risk_factors": []}}
    return SimpleNamespace(model="claude-sonnet-5", id="synthetic", stop_reason=stop,
        usage=SimpleNamespace(input_tokens=10, output_tokens=100),
        content=[SimpleNamespace(type="tool_use", name="trading_consensus", input=body)])


@pytest.fixture
def setup_probe(tmp_path, monkeypatch):
    create = AsyncMock(return_value=response())
    wrapper = SimpleNamespace(messages=SimpleNamespace(create=create))
    model = AnthropicSwarmClient(client=wrapper)
    asyncio.run(model.generate_trading_consensus("PRIVATE_SAVED_PROMPT"))
    req = create.await_args.kwargs
    saved = {"schema": "pramana.inference.v1", "provider": "anthropic",
             "request": req, "request_sha256": content_hash(req)}
    folder = tmp_path / "proofs"
    folder.mkdir()
    source = folder / "proof.json"
    source.write_text(json.dumps(saved))
    db = tmp_path / "ai-budget.sqlite"
    budget = SqliteAIBudget(db, daily_call_limit=1, daily_token_limit=2000)
    budget.close()
    for name, value in {"TRADING_LIVE_MONEY_ACTIVE": "false", "ANTHROPIC_API_KEY": "PRIVATE_KEY",
                        "PRAMANA_PROOF_DIR": str(folder), "PRAMANA_AI_BUDGET_DB": str(db),
                        "PRAMANA_AI_DAILY_CALL_LIMIT": "1", "PRAMANA_AI_DAILY_TOKEN_LIMIT": "2000"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    sdk = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response())), close=AsyncMock())
    factory = {}
    def build(**kwargs):
        factory.update(kwargs)
        return sdk
    monkeypatch.setattr("anthropic.AsyncAnthropic", build)
    return source, saved, sdk, factory


def test_acknowledgement_precedes_any_probe(monkeypatch, capsys):
    called = AsyncMock()
    monkeypatch.setattr(probe, "run_probe", called)
    assert probe.main([]) == 2
    called.assert_not_called()
    assert "no provider call" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["true", "1", "", "unknown"])
def test_live_or_unknown_mode_is_refused(setup_probe, monkeypatch, mode):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", mode)
    with pytest.raises(probe.ProbeRefused, match="explicit_paper_mode_required"):
        asyncio.run(probe.run_probe())
    setup_probe[2].messages.create.assert_not_called()


def test_one_exact_request_fixed_endpoint_and_no_private_output(setup_probe, monkeypatch):
    source, saved, sdk, factory = setup_probe
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://untrusted.invalid")
    result = asyncio.run(probe.run_probe())
    sdk.messages.create.assert_awaited_once_with(**saved["request"])
    assert factory["base_url"] == "https://api.anthropic.com"
    assert factory["max_retries"] == 0
    assert result["request_attempts"] == 1 and result["orders_enabled"] is False
    assert result["validation_status"] == "completed"
    assert "PRIVATE_" not in json.dumps(result)
    assert json.loads(source.read_text()) == saved
    assert not list(source.parent.parent.glob("*ledger*"))
    sdk.close.assert_awaited_once()


def test_tampered_request_hash_refuses_before_provider(setup_probe):
    source, saved, sdk, _ = setup_probe
    saved["request_sha256"] = "0" * 64
    source.write_text(json.dumps(saved))
    with pytest.raises(probe.ProbeRefused, match="saved_request_hash_mismatch"):
        asyncio.run(probe.run_probe())
    sdk.messages.create.assert_not_called()


def test_valid_hash_different_protocol_still_does_not_send(setup_probe):
    source, saved, sdk, _ = setup_probe
    saved["request"]["max_tokens"] = 12000
    saved["request_sha256"] = content_hash(saved["request"])
    source.write_text(json.dumps(saved))
    result = asyncio.run(probe.run_probe())
    assert result["request_attempts"] == 0
    assert result["validation_status"] == "unavailable"
    sdk.messages.create.assert_not_called()


def test_budget_disabled_refuses_without_network(setup_probe, monkeypatch):
    monkeypatch.setenv("PRAMANA_AI_DAILY_CALL_LIMIT", "0")
    with pytest.raises(probe.ProbeRefused, match="enabled_ai_budget_required"):
        asyncio.run(probe.run_probe())
    setup_probe[2].messages.create.assert_not_called()


def test_missing_budget_does_not_create_it(setup_probe):
    source, _, sdk, _ = setup_probe
    db = source.parent.parent / "ai-budget.sqlite"
    db.unlink()
    with pytest.raises(probe.ProbeRefused, match="existing_ai_budget_required"):
        asyncio.run(probe.run_probe())
    assert not db.exists()
    sdk.messages.create.assert_not_called()


def test_truncation_is_reported_not_retried(setup_probe):
    sdk = setup_probe[2]
    sdk.messages.create.return_value = response("max_tokens")
    report = asyncio.run(probe.run_probe())
    assert report["validation_status"] == "invalid_schema"
    assert report["stop_reason"] == "max_tokens"
    assert report["validation_code"] == "output_truncated"
    assert report["request_attempts"] == 1
    sdk.messages.create.assert_awaited_once()


def test_missing_key_refuses_before_sdk(setup_probe, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(probe.ProbeRefused, match="anthropic_key_missing"):
        asyncio.run(probe.run_probe())
    setup_probe[2].messages.create.assert_not_called()


def test_shape_redacts_provider_text_and_unknown_keys():
    data = response("PRIVATE_STOP")
    data.content[0].input["PRIVATE_FIELD"] = "PRIVATE_VALUE"
    shape = probe.response_shape(data)
    assert shape["stop_reason"] == "unknown"
    assert shape["unknown_field_count"] == 1
    assert "PRIVATE" not in json.dumps(shape)


@pytest.mark.parametrize("stop,expected", [("max_tokens", 1), ("refusal", 1),
                                          (None, 1), ("tool_use", 0)])
def test_cli_never_certifies_incomplete_envelope(monkeypatch, capsys, stop, expected):
    report = {"validation_status": "completed", "stop_reason": stop,
              "tool_blocks": 1, "matching_tool_blocks": 1}
    monkeypatch.setattr(probe, "run_probe", AsyncMock(return_value=report))
    assert probe.main(["--confirm-paid-call"]) == expected
    assert json.loads(capsys.readouterr().out)["diagnostic_passed"] is (expected == 0)
