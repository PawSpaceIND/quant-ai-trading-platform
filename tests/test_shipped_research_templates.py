"""The experiment templates an operator is handed must be ones the lab accepts.

A shipped example the code refuses is worse than no example: it is read as the documented
shape, fails on a real database, and sends the reader looking for their own mistake. The
lab validates hard - distinct candidates, a baseline among them, paper-only mode, positive
budgets, cost assumptions under 10000 bps, a protocol version, quote age inside the
configured window, NSE equity or ETF in INR, and source provenance no later than the
decision. Every one of those is a way the template could be wrong on arrival.

These run the shipped files through the real ``ResearchLab``. No provider is contacted:
``create`` and ``add_case`` are local, and the paid path is never reached here.
"""

import json
from pathlib import Path

import pytest

from quant_ai.research.lab import ResearchLab

CONFIG = Path("deploy/research-experiment.example.json")
CASE = Path("deploy/research-case.example.json")


@pytest.fixture
def lab(tmp_path):
    instance = ResearchLab(tmp_path / "research.sqlite")
    yield instance
    instance.close()


def config():
    return json.loads(CONFIG.read_text())


def packet():
    return json.loads(CASE.read_text())


def test_the_shipped_experiment_config_is_one_the_lab_accepts(lab):
    lab.create("shipped", config())
    stored = lab.config("shipped")
    assert stored["candidates"] == ["claude", "astra", "cash"]
    assert stored["baseline"] == "cash"
    # Paper only. The lab refuses any other mode, and a template that names one would
    # hand an operator a file that cannot be used.
    assert stored["mode"] in ("historical", "forward_paper")


def test_the_shipped_case_packet_is_one_the_lab_accepts(lab):
    lab.create("shipped", config())
    lab.add_case("shipped", "case-1", packet())
    body = lab.export_evidence("shipped")["body"]
    assert [case["case_id"] for case in body["cases"]] == ["case-1"]


def test_the_two_comparison_candidates_are_the_two_the_runner_will_be_given(lab):
    """``evaluate_case`` refuses unless the provider set is exactly the non-baseline names.

    ``scripts/research_extensions.py compare`` builds that set from ``config["providers"]``,
    so a manifest naming the wrong candidates - or forgetting that the baseline takes no
    provider - fails at the runner rather than in the template, where it is visible.
    """
    declared = config()
    comparison = set(declared["candidates"]) - {declared["baseline"]}
    assert set(declared["providers"]) == comparison == {"claude", "astra"}
    assert declared["baseline"] not in declared["providers"], "the cash baseline is not paid for"


def test_both_candidates_name_a_provider_the_runner_supports():
    """Only ``openai`` and ``anthropic`` construct; anything else raises on the first run."""
    from quant_ai.research.providers import Provider

    for name, settings in config()["providers"].items():
        assert settings["provider"] in ("openai", "anthropic"), name
        # Constructs, so the manifest cannot name a shape ``Provider`` rejects.
        Provider(settings["provider"], settings["model"], key="test-only")


def test_the_rate_table_covers_every_field_the_cost_calculation_reads():
    """Missing a rate does not raise - it silently records cost as unknown.

    ``usage_cost`` falls back to ``api_cost_usd=None`` on any KeyError, so an incomplete
    table produces a comparison with no spend attached and nothing saying why. The fields
    are pinned here instead.
    """
    required = {
        "input_per_million", "output_per_million",
        "cached_input_per_million", "cache_creation_per_million",
    }
    rates = config()["provider_rates"]
    assert set(rates) == {"claude", "astra"}
    for name, table in rates.items():
        assert set(table) == required, name


def test_the_cost_assumptions_are_stated_and_not_free(lab):
    """A comparison run at zero cost is a comparison that proves nothing.

    Both candidates are charged the same friction on entry and exit, so the costs do not
    decide the winner - but a template shipped at 0 bps would flatter every candidate
    against the cash baseline, which is the one comparison that matters.
    """
    from decimal import Decimal

    declared = config()
    assert Decimal(declared["fee_bps"]) > 0
    assert Decimal(declared["slippage_bps"]) > 0
    assert Decimal(declared["capital_per_case"]) > 0
