"""What the operator CLI is allowed to claim about the engine it built.

``build_runtime`` serves ``portfolio``, ``analytics`` and ``stress-test``. It opens the
real paper ledger but is fed by a *sandbox* price feed on a hardcoded US instrument, so a
single command mixes genuine account state with generated prices. Every test here is
about that seam being visible in the output rather than inferred from the source.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from test_attribution_memory import AGENTS

from quant_ai.analytics import decision_journal as journal
from quant_ai.cli import _analytics, build_runtime, main

SESSION = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("QUANT_AI_TENANT_ID", "ghost")
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    return tmp_path


def closed_trade(broker, decision_id, *, pnl, minutes):
    journal.insert_decision(broker, {
        "decision_id": decision_id, "tenant_id": "ghost", "symbol": "INFY", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": (SESSION + timedelta(minutes=minutes)).isoformat(),
        "stance": "BUY", "side": "BUY", "confidence": "0.7", "expected_return": "0.01",
        "expected_risk": "0.005", "reference_price": "100", "stop_price": "95",
        "take_profit_price": "110", "regime": "trending_up", "mode": "llm",
        "governance": "filled", "reason": None, "order_id": decision_id,
        "agents": json.dumps(AGENTS), "realized_net_pnl": str(pnl),
        "exit_at": (SESSION + timedelta(minutes=minutes + 1)).isoformat(),
    })


def printed(capsys):
    return dict(
        line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line
    )


def test_learning_already_earned_is_reported_rather_than_rebuilt_as_nothing(workspace, capsys):
    """The failure this pins, which looked exactly like an honest zero.

    The pilot daemon rebuilds specialist scores from the decision journal at boot. This
    runtime did not, so its attribution engine was always empty and ``analytics`` always
    printed ``agent_attribution=none`` - the same line an account with no closed trades
    prints, and the reason an operator could not tell "nothing learned yet" from "this
    command never looked". A command that cannot distinguish the two states must not be
    read as evidence for either.
    """
    seed = build_runtime()
    for index in range(3):
        closed_trade(seed.tracker.broker, f"d{index}", pnl=100, minutes=index * 10)

    _analytics(build_runtime())
    out = printed(capsys)
    assert "agent_attribution" not in out, "closed trades were in the journal"
    assert out["agent"].startswith("technical hit_rate=1")


def test_an_account_with_no_closed_trades_still_reports_none(workspace, capsys):
    """The other half: restoring must not invent a score where the journal has none."""
    _analytics(build_runtime())
    assert printed(capsys)["agent_attribution"] == "none"


def test_generated_prices_are_named_as_generated(workspace, capsys):
    """The ratios are arithmetic on a synthetic series, not market observation.

    The feed here produces prices from a base of 200 with no market involved, so every
    ratio is reproducible to the last digit on any machine and describes nothing that
    happened. Saying so is the difference between a demo and a measurement.
    """
    _analytics(build_runtime())
    out = printed(capsys)
    assert out["series_source"] == "sandbox_generated_prices"
    assert out["series"] == "instrument_price:AAPL:USA:1m:last_60_minutes"


def test_a_market_flag_this_runtime_cannot_honour_is_refused_not_ignored(workspace, capsys):
    """``analytics --market india`` printed AAPL figures under an India flag.

    ``--market`` is read only by ``backtest``; every other command builds the same US
    sandbox instrument regardless. Accepting the flag and ignoring it is how an operator
    ends up reading US synthetic statistics as their India pilot's.
    """
    for command in ("analytics", "portfolio", "stress-test"):
        with pytest.raises(SystemExit) as refused:
            main([command, "--market", "india"])
        assert "--market does not apply" in str(refused.value)


def test_backtest_still_reads_the_flag_it_owns(workspace):
    # Removing the argparse default must not silently switch backtest to a US instrument.
    with pytest.raises(SystemExit) as missing:
        main(["backtest", "--market", "india"])
    assert "backtest requires --data" in str(missing.value)
