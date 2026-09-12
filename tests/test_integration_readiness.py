from quant_ai.config.runtime import RuntimeConfig, RuntimeMode
from quant_ai.integrations.readiness import blockers, readiness_for_mode


def test_paper_requires_market_data_and_paper_broker() -> None:
    statuses = readiness_for_mode(RuntimeMode.PAPER, {"primary_market_data"})
    assert blockers(statuses) == ("missing_required_integration:paper_broker",)


def test_live_requires_explicit_runtime_opt_in() -> None:
    try:
        RuntimeConfig(mode=RuntimeMode.LIVE).validate()
    except ValueError as exc:
        assert "allow_live_orders" in str(exc)
    else:
        raise AssertionError("live config must fail closed")
