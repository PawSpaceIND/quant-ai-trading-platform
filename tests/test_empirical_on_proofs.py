"""Measured payoffs priced beside the declared EV on every proof, and read by nothing.

The live expected value keeps the specialists' declared 1% / 2%. What this adds is the
second number, ``ev_empirical``: the same probability and cost priced against payoffs the
market actually produced, from an artifact the operator wrote with
``pilot_ops.py empirical-payoffs`` and named in ``PRAMANA_EMPIRICAL_PAYOFFS``. It is read
once at boot, verified against its own id, and never sizes, gates or replaces anything.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.expected_value import DECLARED
from quant_ai.analytics import empirical_payoffs as payoffs
from quant_ai.operations.premarket import _empirical_payoffs as premarket_line

MOMENT = datetime(2026, 9, 22, 5, tzinfo=timezone.utc)


def journal(symbol: str = "INFY", count: int = 40, *, playbook: str = "range_trading",
            regime: str = "ranging") -> list[dict]:
    """Resolved rows: three in four moved +1.2%, the rest -0.4%, at a 10 bp stored cost."""
    return [{
        "decision_id": f"{symbol}-{index}", "symbol": symbol, "playbook": playbook, "regime": regime,
        "decided_at": (MOMENT - timedelta(minutes=index)).isoformat(),
        "forecast_horizon_seconds": 3600, "forecast_cost_bps": "10",
        "forward_return_60m": "0.012" if index % 4 else "-0.004",
    } for index in range(count)]


def written(tmp_path, rows, name="payoffs.json"):
    found = payoffs.artifact(rows, generated_at=MOMENT)
    path = tmp_path / name
    path.write_text(json.dumps(found, indent=2, sort_keys=True))
    return path, found


def lean(symbol: str = "INFY") -> tuple[AgentEvidence, ...]:
    """Three specialists BUY at 0.60: a full entry, declared EV -0.0015 at 1% / 2%."""
    return tuple(
        AgentEvidence(agent, domain, symbol, Stance.BUY, Decimal("0.60"), Decimal("0.01"),
                      Decimal("0.02"), ("declared",), MOMENT, 10)
        for agent, domain in (("technical-quant-mas", AgentDomain.TECHNICAL),
                              ("geopolitical-analyst", AgentDomain.NEWS),
                              ("indian-equities", AgentDomain.COUNTRY))
    )


# ------------------------------------------------------------------ the artifact


def test_a_written_artifact_loads_and_names_its_own_measurement(tmp_path) -> None:
    path, found = written(tmp_path, journal())
    assert payoffs.load_artifact(path) == found
    measured = payoffs.measured_payoffs(found)(symbol="INFY")
    assert measured["source"] == f"empirical:{found['id']}"
    assert (measured["e_win_hat"], measured["e_loss_hat"], measured["n"]) == ("0.011000", "0.005000", 40)


@pytest.mark.parametrize("damage, reason", [
    (lambda found: found["by_symbol"]["INFY"].update(e_win_hat="0.050000"), "does not match its id"),
    (lambda found: found.update(schema="pramana.empirical_payoffs.v2"), "not a pramana.empirical_payoffs.v1"),
    (lambda found: found.update(id="0000000000000000"), "does not match its id"),
])
def test_an_edited_or_foreign_artifact_is_refused(tmp_path, damage, reason) -> None:
    """An id names one measurement. A file whose numbers moved under it names nothing."""
    path, found = written(tmp_path, journal())
    damage(found)
    path.write_text(json.dumps(found))
    with pytest.raises(payoffs.ArtifactError, match=reason):
        payoffs.load_artifact(path)


def test_a_missing_or_unparseable_file_is_refused_in_words(tmp_path) -> None:
    with pytest.raises(payoffs.ArtifactError, match=r"unreadable \(FileNotFoundError\)"):
        payoffs.load_artifact(tmp_path / "absent.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(payoffs.ArtifactError, match=r"unreadable \(JSONDecodeError\)"):
        payoffs.load_artifact(tmp_path / "bad.json")


def test_a_thin_group_prices_nothing_and_the_playbook_group_stands_in_for_an_unmeasured_symbol(tmp_path) -> None:
    rows = journal("INFY", 40) + journal("TRENT", 12)
    _, found = written(tmp_path, rows)
    lookup = payoffs.measured_payoffs(found)
    # TRENT has 12 rows of its own - thin - so the range_trading:ranging group (52) answers.
    trent = lookup(symbol="TRENT", playbook="range_trading", regime="ranging")
    assert trent["n"] == 52 and trent["source"] == f"empirical:{found['id']}"
    assert lookup(symbol="TRENT", playbook="defensive", regime="trending_down") is None


def test_a_group_under_this_builds_floor_prices_nothing_whatever_its_file_says(tmp_path) -> None:
    """A file written by a build with a lower floor marks 12 rows as not thin. Not here."""
    _, found = written(tmp_path, journal("INFY", 12))
    content = {key: found[key] for key in payoffs.CONTENT_KEYS}
    content["minimum_sample"] = 10
    content["by_symbol"]["INFY"]["thin"] = False
    lax = {**content, "id": payoffs._digest(content)}
    path = tmp_path / "lax.json"
    path.write_text(json.dumps(lax))
    assert payoffs.measured_payoffs(payoffs.load_artifact(path))(symbol="INFY") is None


def test_the_environment_names_the_artifact_or_says_it_is_off(tmp_path) -> None:
    path, found = written(tmp_path, journal())
    lookup, status = payoffs.from_env({payoffs.ARTIFACT_ENV: str(path)})
    assert lookup is not None and lookup(symbol="INFY")["source"] == f"empirical:{found['id']}"
    assert status.startswith(f"empirical:{found['id']} attached to proofs as ev_empirical; 2 group(s) past the 30-row floor")
    assert status.endswith("recorded only, the live EV stays declared")
    assert payoffs.from_env({}) == (None, "off (set PRAMANA_EMPIRICAL_PAYOFFS to a file written by pilot_ops.py empirical-payoffs)")
    assert payoffs.from_env({payoffs.ARTIFACT_ENV: str(tmp_path / "gone.json")}) == (
        None, "not attached: unreadable (FileNotFoundError)")


def test_the_premarket_check_says_which_measurement_is_attached_and_never_fails(tmp_path) -> None:
    path, found = written(tmp_path, journal())
    line = premarket_line({payoffs.ARTIFACT_ENV: str(path)})
    assert (line.id, line.state) == ("empirical_payoffs", "INFO")
    assert line.detail.startswith(f"empirical:{found['id']} attached")
    assert premarket_line({}).state == premarket_line({payoffs.ARTIFACT_ENV: "/nope.json"}).state == "INFO"
    assert premarket_line({payoffs.ARTIFACT_ENV: "/nope.json"}).detail.startswith("not attached")


# ------------------------------------------------------------------ the decision and the proof


def decide(policy: AtlasPolicy | None = None, lookup=None):
    agent = AtlasInvestmentAgent(policy=policy, empirical_payoffs=lookup)
    decision = agent.decide("INFY", lean(), MOMENT)
    return decision, decision.provenance["expected_value"]


def test_the_measured_ev_rides_beside_a_declared_ev_it_never_changes(tmp_path) -> None:
    _, found = written(tmp_path, journal())
    declared_decision, declared = decide()
    decision, block = decide(lookup=payoffs.measured_payoffs(found))
    assert decision.action == declared_decision.action
    # Every declared field is byte-for-byte what it was without the artifact.
    assert {key: value for key, value in block.items() if key != "ev_empirical"} == declared
    assert block["e_win_source"] == block["e_loss_source"] == DECLARED
    assert block["expected_value"] == "-0.001500"
    # p = 0.65 (the declared -0.0015 is 0.65 * 0.01 - 0.35 * 0.02 - 0.001), so measured:
    # 0.65 * 0.011 - 0.35 * 0.005 - 0.001 = 0.0044. The sign flips on measured payoffs.
    assert block["ev_empirical"] == {
        "expected_win": "0.011000", "expected_loss": "-0.005000", "expected_value": "0.004400",
        "e_win_source": f"empirical:{found['id']}", "e_loss_source": f"empirical:{found['id']}",
        "sample": 40,
    }


def test_an_armed_gate_reads_the_declared_ev_even_when_the_measured_one_is_positive(tmp_path) -> None:
    """The measurement has to earn its way in. Until then it cannot un-hold anything."""
    _, found = written(tmp_path, journal())
    decision, block = decide(AtlasPolicy(ev_gate=True), payoffs.measured_payoffs(found))
    assert Decimal(block["ev_empirical"]["expected_value"]) > 0 > Decimal(block["expected_value"])
    assert decision.action is Stance.NEUTRAL
    assert decision.provenance["ev_gate"] == {"armed": True, "expected_value": "-0.001500", "held": True}


def test_the_measured_ev_reaches_the_proof_file(tmp_path) -> None:
    from test_decision_journal import TENANT, pilot_broker, plan

    from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent
    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot
    from quant_ai.execution.audit import XAITraceLogger

    _, found = written(tmp_path, journal())
    agent = AtlasInvestmentAgent(empirical_payoffs=payoffs.measured_payoffs(found))
    request = AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, MOMENT, {})
    proposal = AtlasCIOAgent(agent).propose(
        request, lean(), quantity=10, reference_price=Decimal(100), stop_price=Decimal(95),
        take_profit_price=Decimal(120), country="India",
    )
    proofs = tmp_path / "proofs"
    runtime = SwarmPaperTradingService(broker=pilot_broker(tmp_path), xai_logger=XAITraceLogger(proofs))
    runtime._execute_proposal(request, lean(), proposal, plan(), PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0)), None, TENANT)
    proof = json.loads((proofs / f"{proposal.decision_id}.json").read_text())
    valued = proof["provenance"]["expected_value"]
    assert valued["e_win_source"] == DECLARED and valued["expected_value"] == "-0.001500"
    assert valued["ev_empirical"]["e_win_source"] == f"empirical:{found['id']}"


# ------------------------------------------------------------------ the engine


def test_the_engine_attaches_the_operators_artifact_at_boot(tmp_path, monkeypatch, caplog) -> None:
    from test_pilot_closure import runner_for

    path, found = written(tmp_path, journal())
    monkeypatch.setenv(payoffs.ARTIFACT_ENV, str(path))
    with caplog.at_level(logging.INFO, logger="quant_ai.ghost_runner"):
        runner = runner_for(tmp_path)
    lookup = runner.daemon.scheduler.pipeline.runtime.cio.atlas.empirical_payoffs
    assert lookup(symbol="INFY")["source"] == f"empirical:{found['id']}"
    assert f"empirical_payoffs empirical:{found['id']} attached" in caplog.text


def test_an_artifact_that_cannot_be_attached_is_logged_and_the_engine_still_boots(tmp_path, monkeypatch, caplog) -> None:
    from test_pilot_closure import runner_for

    monkeypatch.setenv(payoffs.ARTIFACT_ENV, str(tmp_path / "missing.json"))
    with caplog.at_level(logging.INFO, logger="quant_ai.ghost_runner"):
        runner = runner_for(tmp_path)
    assert runner.daemon.scheduler.pipeline.runtime.cio.atlas.empirical_payoffs is None
    assert "empirical_payoffs not attached: unreadable (FileNotFoundError)" in caplog.text


def test_with_no_setting_the_engine_attaches_nothing(tmp_path, monkeypatch) -> None:
    from test_pilot_closure import runner_for

    monkeypatch.delenv(payoffs.ARTIFACT_ENV, raising=False)
    assert runner_for(tmp_path).daemon.scheduler.pipeline.runtime.cio.atlas.empirical_payoffs is None


def test_compose_forwards_the_setting_empty_by_default() -> None:
    from pathlib import Path

    import yaml

    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "deploy/docker-compose.yml").read_text())
    assert compose["services"]["pramana-ghost"]["environment"][payoffs.ARTIFACT_ENV] == "${PRAMANA_EMPIRICAL_PAYOFFS:-}"
