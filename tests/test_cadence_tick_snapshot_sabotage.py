"""Offline guard-removal checks on disposable source copies, never on the checkout."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STREAM = "src/quant_ai/marketdata/ticker_stream.py"
READER = "src/quant_ai/orchestration/cadence.py"
TEST = "tests/test_cadence_tick_snapshot.py"

# Each exact anchor is deliberately small; source drift requires explicit review.
MUTATIONS = [
    (
        'latest_cutoff',
        'src/quant_ai/marketdata/ticker_stream.py',
        'if latest is None or utc_time(latest.observed_at) <= current:',
        'if True:',
        'test_newer_arrival_must_not_make_the_eligible_cutoff_tick_future',
    ),
    (
        'retained_latest',
        'src/quant_ai/marketdata/ticker_stream.py',
        'if latest is None or utc_time(latest.observed_at) <= current:\n                return latest',
        'if latest is None or utc_time(latest.observed_at) <= current:\n                return None',
        'test_old_latest_for_other_symbol_survives_shared_history_eviction',
    ),
    (
        'newest_order',
        'src/quant_ai/marketdata/ticker_stream.py',
        'for tick in reversed(self._ticks):',
        'for tick in self._ticks:',
        'test_snapshot_itself_enforces_symbol_cutoff_and_latest_revision',
    ),
    (
        'history_symbol',
        'src/quant_ai/marketdata/ticker_stream.py',
        'if tick.symbol == symbol and utc_time(tick.observed_at) <= current:',
        'if utc_time(tick.observed_at) <= current:',
        'test_snapshot_itself_enforces_symbol_cutoff_and_latest_revision',
    ),
    (
        'history_cutoff',
        'src/quant_ai/marketdata/ticker_stream.py',
        'if tick.symbol == symbol and utc_time(tick.observed_at) <= current:',
        'if tick.symbol == symbol:',
        'test_snapshot_itself_enforces_symbol_cutoff_and_latest_revision',
    ),
    (
        'inclusive_cutoff',
        'src/quant_ai/marketdata/ticker_stream.py',
        'if tick.symbol == symbol and utc_time(tick.observed_at) <= current:',
        'if tick.symbol == symbol and utc_time(tick.observed_at) < current:',
        'test_tick_arriving_while_headlines_await_keeps_original_cutoff',
    ),
    (
        'eviction_refusal',
        'src/quant_ai/marketdata/ticker_stream.py',
        '            return None\n\n    def snapshot(self)',
        '            return self._latest.get(symbol)\n\n    def snapshot(self)',
        'test_snapshot_eviction_itself_returns_none_not_post_cutoff_tick',
    ),
    (
        'reader_selection',
        'src/quant_ai/orchestration/cadence.py',
        '        if deferred:\n            # Preserve',
        '        if False:\n            # Preserve',
        'test_newer_arrival_must_not_make_the_eligible_cutoff_tick_future',
    ),
    (
        'original_cutoff',
        'src/quant_ai/orchestration/cadence.py',
        'select(symbol, current)',
        'select(symbol, newest_at)',
        'test_newer_arrival_must_not_make_the_eligible_cutoff_tick_future',
    ),
    (
        'initial_integrity',
        'src/quant_ai/orchestration/cadence.py',
        '\n        if tick_value_issue(tick) or tick.symbol != symbol:',
        '\n        if False:',
        'test_invalid_latest_is_not_laundered_through_valid_history',
    ),
    (
        'callable_history',
        'src/quant_ai/orchestration/cadence.py',
        'if callable(select) else None',
        'if select is not None else None',
        'test_noncallable_history_attribute_cannot_admit_future_tick',
    ),
    (
        'selector_failure',
        'src/quant_ai/orchestration/cadence.py',
        '            except (TypeError, ValueError, AttributeError):\n                return None, "Invalid Market Data"\n        if tick is None:',
        '            except (TypeError, ValueError, AttributeError):\n                return tick, None\n        if tick is None:',
        'test_failed_history_read_stays_invalid',
    ),
    (
        'selected_values',
        'src/quant_ai/orchestration/cadence.py',
        '            if tick_value_issue(tick) or tick.symbol != symbol:',
        '            if tick.symbol != symbol:',
        'test_history_selector_result_is_revalidated',
    ),
    (
        'selected_symbol',
        'src/quant_ai/orchestration/cadence.py',
        '            if tick_value_issue(tick) or tick.symbol != symbol:',
        '            if tick_value_issue(tick):',
        'test_history_selector_result_is_revalidated',
    ),
    (
        'selected_future',
        'src/quant_ai/orchestration/cadence.py',
        '            elif observed > current:',
        '            elif False:',
        'test_history_selector_result_is_revalidated',
    ),
    (
        'selected_staleness',
        'src/quant_ai/orchestration/cadence.py',
        '            elif current - observed > self.max_tick_age:',
        '            elif False:',
        'test_asof_selection_keeps_original_two_minute_age_limit',
    ),
    (
        'no_history_refusal',
        'src/quant_ai/orchestration/cadence.py',
        '        if tick is None:\n            issue = "Future Market Data"',
        '        if tick is None:\n            issue = None',
        'test_no_pre_cutoff_tick_remains_refused',
    ),
    (
        'time_diagnostic',
        'src/quant_ai/orchestration/cadence.py',
        '            LOGGER.info(',
        '            LOGGER.debug(',
        'test_cutoff_diagnostic_records_times_not_prices',
    ),
    (
        'snapshot_lock',
        'src/quant_ai/marketdata/ticker_stream.py',
        '        with self._lock:\n            latest = self._latest.get(symbol)',
        '        if True:\n            latest = self._latest.get(symbol)',
        'test_snapshot_uses_the_same_lock_as_ingestion',
    ),
]


@pytest.mark.parametrize("case", MUTATIONS, ids=[item[0] for item in MUTATIONS])
def test_cutoff_guard_mutation_is_detected_without_touching_checkout(tmp_path, case):
    name, relative, anchor, replacement, regression = case
    protected = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
                 for p in (STREAM, READER, TEST)}
    source = (ROOT / relative).read_text()
    assert source.count(anchor) == 1, f"Review changed mutation anchor: {name}"
    work = tmp_path / "isolated"
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (work / "tests").mkdir()
    shutil.copy2(ROOT / TEST, work / TEST)
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "PYTHONPATH": str(work / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    launcher = (
        "import sys\n"
        "def deny(event, args):\n"
        "    if event in ('socket.connect','socket.connect_ex','socket.getaddrinfo','socket.sendto'):\n"
        "        raise RuntimeError('offline_cutoff_mutation_network_forbidden')\n"
        "sys.addaudithook(deny)\n"
        "import pytest\n"
        "raise SystemExit(pytest.main(sys.argv[1:]))\n"
    )

    def run(label):
        xml = work / (label + ".xml")
        result = subprocess.run(
            [sys.executable, "-B", "-c", launcher, "-q", TEST + "::" + regression,
             "--junitxml=" + str(xml)], cwd=work, env=env,
            capture_output=True, text=True, timeout=45, check=False,
        )
        root = ET.parse(xml).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        counts = {k: sum(int(s.get(k, 0)) for s in suites)
                  for k in ("tests", "failures", "errors", "skipped")}
        (work / (label + ".log")).write_text(result.stdout + result.stderr)
        return result, counts

    control, good = run("control")
    assert control.returncode == 0 and good["tests"] > 0
    assert good["failures"] == good["errors"] == good["skipped"] == 0
    target = work / relative
    try:
        target.write_text(source.replace(anchor, replacement, 1))
        mutant, broken = run("mutant")
        assert mutant.returncode == 1, mutant.stdout + mutant.stderr
        assert broken["failures"] > 0 and broken["errors"] == broken["skipped"] == 0
    finally:
        target.write_text(source)
    assert {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in protected} == protected
