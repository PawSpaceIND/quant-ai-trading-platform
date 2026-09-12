from quant_ai.operations.readiness import ReadinessCheck, ReadinessReport


def test_readiness_reports_blockers() -> None:
    report = ReadinessReport((ReadinessCheck("market_data", True), ReadinessCheck("broker", False, "missing credentials")))
    assert not report.ready
    assert report.blockers[0].name == "broker"
