"""Synthetic fixtures exercise the actual publisher, never market performance claims."""
from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant_ai.backtesting import research_publisher as pub
from quant_ai.backtesting.baselines import BaselineEvaluator, TimeSeriesMomentumBaseline
from quant_ai.backtesting.history import SymbolHistory, build_payload, write_dataset
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.validation.trial_register import record_trials, register_summary

D = Decimal
NOW = datetime(2026, 9, 17, tzinfo=timezone.utc)
INSTRUMENT = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
STUDY = "replay:INFY:INDIA"
ROOT = Path(__file__).resolve().parents[1]


def series(count=1200):
    result = []
    previous = D(100)
    for index in range(count):
        close = D(str(round(100 + 15 * math.sin(index * math.pi / 6), 6)))
        result.append(Candle(
            INSTRUMENT, datetime(2010, 1, 1, tzinfo=timezone.utc) + timedelta(days=index),
            previous, max(previous, close) + 1, min(previous, close) - 1,
            close, D(2000000),
        ))
        previous = close
    return tuple(result)


def seeded_register(folder, bars):
    path = folder / "trial-register.jsonl"
    for window in range(20):
        inspected = bars[window:600]
        record_trials(
            path, study=STUDY, candidate_trials=1,
            configuration={"start": inspected[0].timestamp.isoformat(),
                           "end": inspected[-1].timestamp.isoformat()},
            data_sha256=pub.bar_digest(inspected), now=NOW,
        )
    return path


def publish(folder, bars=None):
    bars = series() if bars is None else bars
    return pub.publish_research(
        bars, instrument=INSTRUMENT, register=seeded_register(folder, bars),
        output=folder / "report.json", now=NOW,
    )


@pytest.fixture(autouse=True)
def paper_only(monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    return publish(tmp_path_factory.mktemp("synthetic-research"))


def producer_dataset(folder):
    bars = series()
    history = SymbolHistory(INSTRUMENT, bars, ())
    payload = build_payload(history, history, requested_start=bars[0].timestamp,
                            requested_end=NOW, years=4, fetched_at=NOW)
    payload["provenance"]["source"] = "synthetic-test-fixture-not-market-evidence"
    return write_dataset(folder / "INFY.json", payload)


def test_real_history_producer_is_accepted_without_relabelling(tmp_path, monkeypatch):
    source = producer_dataset(tmp_path)
    original = source.read_bytes()
    captured = {}

    def capture(bars, **kwargs):
        captured["bars"] = bars
        captured.update(kwargs)
        return {"report_sha256": "a" * 64, "candidate_trials": 23}

    monkeypatch.setattr(pub, "publish_research", capture)
    monkeypatch.setenv("PRAMANA_RESEARCH_REPORT", str(tmp_path / "report.json"))
    args = SimpleNamespace(data=str(source), market="india", start=None, end=None)
    assert pub.publish_from_args(args) == 0
    assert captured["bars"] == series()
    assert captured["source_file_sha256"] == hashlib.sha256(original).hexdigest()
    assert source.read_bytes() == original


def test_actual_publisher_counts_all_trials_and_only_holdout(report):
    assert report["schema"] == "pramana.research.v1"
    assert report["candidate_trials"] == 23
    assert report["holdout"]["observations"] == 359
    assert report["holdout"]["round_trips"] >= 20
    assert report["buy_and_hold"]["observations"] == 359
    assert report["limitations"]
    assert report["is_the_traded_ai"] is False
    assert report["automatic_promotion"] is False
    expected = hashlib.sha256(json.dumps(
        {k: v for k, v in report.items() if k != "report_sha256"},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    assert report["report_sha256"] == expected


def test_fit_and_selection_never_see_holdout(tmp_path, monkeypatch):
    original = BaselineEvaluator.run
    calls = []

    def spy(self, rule, bars, **kwargs):
        calls.append((rule, bars))
        return original(self, rule, bars, **kwargs)

    monkeypatch.setattr(BaselineEvaluator, "run", spy)
    actual = publish(tmp_path)
    bars = series()
    assert len(calls) == 5
    assert all(data == bars[:840] for _, data in calls[:3])
    assert all(data == bars[840:] for _, data in calls[3:])
    independent = original(BaselineEvaluator(instrument=INSTRUMENT), calls[3][0], bars[840:])
    expected = (independent.equity_curve[-1] - D(100000)) / D(100000)
    assert actual["holdout"]["net_return"] == float(expected)
    assert actual["holdout"]["cash_charges"] == str(independent.cash_charges)
    assert independent.cash_charges > 0


def test_full_sample_cannot_be_relabelled_holdout():
    bars = series()
    with pytest.raises(pub.ResearchPublicationRefused):
        pub._assert_split(bars, bars[:840], bars)


@pytest.mark.parametrize("field", ["returns", "round_trips"])
def test_significance_floor_is_mandatory(field):
    run = BaselineEvaluator(instrument=INSTRUMENT).run(TimeSeriesMomentumBaseline(10), series())
    if field == "returns":
        run = replace(run, returns=run.returns[:29])
    else:
        run = replace(run, round_trips=run.round_trips[:19])
    with pytest.raises(pub.ResearchPublicationRefused):
        pub._assert_evidence_floor(run)


def test_independent_bootstrap_not_observed_drawdown():
    returns = (D(".02"), D("-.04"), D(".03"), D(".01"), D("-.015"), D(".02")) * 10
    generator = random.Random(1729)
    losses = []
    for _ in range(1000):
        indices = []
        while len(indices) < len(returns):
            start = generator.randrange(len(returns))
            indices += [(start + i) % len(returns) for i in range(5)]
        equity = peak = D(1)
        loss = D(0)
        for index in indices[:len(returns)]:
            equity *= 1 + returns[index]
            peak = max(peak, equity)
            loss = max(loss, (peak - equity) / peak)
        losses.append(loss)
    actual = pub.path_stress(returns)
    assert actual["max_drawdown_p95"] == float(sorted(losses)[949])
    equity = peak = D(1)
    observed = D(0)
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        observed = max(observed, (peak - equity) / peak)
    assert actual["max_drawdown_p95"] != float(observed)


@pytest.mark.parametrize("case", ["schema", "nan", "infinity", "string", "empty_limitations"])
def test_ui_invalid_report_refused(report, case):
    changed = json.loads(json.dumps(report))
    if case == "schema":
        changed["schema"] = "pramana.contest.v1"
    elif case == "empty_limitations":
        changed["limitations"] = []
    else:
        changed["holdout"]["net_return"] = {"nan": float("nan"), "infinity": float("inf"),
                                             "string": "0.1"}[case]
    with pytest.raises(pub.ResearchPublicationRefused):
        pub.validate_report(changed)


def test_atomic_failure_preserves_previous_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("previous report")

    def fail(*args):
        raise OSError("synthetic rename failure")

    monkeypatch.setattr(pub.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic"):
        pub._atomic_write(output, b"replacement")
    assert output.read_text() == "previous report"
    assert not list(tmp_path.glob(".report.json.*"))


def test_known_prior_holdout_peek_refuses(tmp_path):
    bars = series()
    register = seeded_register(tmp_path, bars)
    record_trials(register, study=STUDY, candidate_trials=1,
                  configuration={"start": bars[0].timestamp.isoformat(),
                                 "end": bars[-1].timestamp.isoformat()},
                  data_sha256=pub.bar_digest(bars), now=NOW)
    with pytest.raises(pub.ResearchPublicationRefused, match="previously_observed"):
        pub.publish_research(bars, instrument=INSTRUMENT, register=register,
                             output=tmp_path / "report.json", now=NOW)
    assert register_summary(register, study=STUDY)["candidate_trials"] == 21
    assert not (tmp_path / "report.json").exists()


def test_paper_only_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    with pytest.raises(pub.ResearchPublicationRefused, match="paper_only"):
        publish(tmp_path)


def test_actual_ui_reader_accepts_publisher_output(tmp_path, report):
    node = shutil.which("node")
    assert node is not None, "Node is required to verify the actual UI reader"
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    javascript = r'''
import fs from 'node:fs';
const [reader, file] = process.argv.slice(1);
const source = fs.readFileSync(reader, 'utf8');
const signature = 'export function readResearch(): ResearchReport | null';
if (!source.includes(signature)) throw new Error('reader signature changed');
// Keep the entire production reader body. Only remove its type-only return annotation
// and supply an isolated ledger path: no database, auth or dashboard server is started.
const body = source.slice(source.indexOf(signature)).replace(signature, 'export function readResearch()');
const imports = 'import fs from "node:fs"; import path from "node:path"; const ledgerPath = () => "unused.db";\n';
const readerModule = await import('data:text/javascript;base64,' + Buffer.from(imports + body).toString('base64'));
process.env.PRAMANA_RESEARCH_REPORT = file;
const actual = readerModule.readResearch();
if (actual === null) throw new Error('actual reader refused publisher output');
for (const mutate of [r => r.schema = 'wrong', r => r.holdout.net_return = 'NaN']) {
  const bad = structuredClone(actual); mutate(bad); fs.writeFileSync(file, JSON.stringify(bad));
  if (readerModule.readResearch() !== null) throw new Error('actual reader accepted invalid report');
}
console.log(actual.report_sha256);
'''
    result = subprocess.run([node, "--input-type=module", "-e", javascript,
                             str(ROOT / "apps/pramana-ui/lib/research.ts"), str(path)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["report_sha256"] in result.stdout
