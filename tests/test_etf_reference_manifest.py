import json

from test_runtime_manifest import setup

from quant_ai.intelligence.etf_reference import ETFReferenceReader


def test_reference_roster_is_supported_and_source_selection_is_bound(tmp_path, monkeypatch):
    runner, source = setup(tmp_path, monkeypatch)
    daemon = runner.daemon
    try:
        initial = daemon.strategy_manifest.capture()
        assert not initial["issues"]
        reference = [a for a in initial["manifest"]["specialists"] if a["agent_id"] == "etf-value-reference"]
        assert len(reference) == 1 and reference[0]["supported"]
        path = tmp_path / "private-reference.json"
        path.write_text('{"observations":[]}')
        daemon.scheduler.pipeline.etf_reference = ETFReferenceReader(path)
        assert daemon.strategy_manifest.check()["status"] == "changed"
        daemon.bind_strategy_manifest(runner.streams, source_root=source, revision="a" * 40)
        before = daemon.strategy_manifest.capture()
        assert not before["issues"]
        assert str(path) not in json.dumps(before)
        path.write_text('{"observations":[{"synthetic":"new observation"}]}')
        assert daemon.strategy_manifest.check()["sha256"] == before["sha256"]
        daemon.scheduler.pipeline.etf_reference = ETFReferenceReader(tmp_path / "other.json")
        assert daemon.strategy_manifest.check()["status"] == "changed"
    finally:
        daemon.tracker.broker.close()


def test_unknown_reference_reader_is_not_silently_accepted(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    try:
        runner.daemon.scheduler.pipeline.etf_reference = object()
        captured = runner.daemon.strategy_manifest.capture()
        assert "unsupported_component:builtins.object" in captured["issues"]
    finally:
        runner.daemon.tracker.broker.close()
