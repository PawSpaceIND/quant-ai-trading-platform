"""The minute-bar warm-up, and the fifty bars three modules independently require.

On 22 September 2026 every decision in the first cadence of the session recorded
``technical-quant-mas`` at zero confidence with "insufficient_price_history", on a host
whose daily history provider was returning a full 120 sessions. The daily bars were never
the input: the analysis pipeline reads at most sixty *one-minute* bars, the specialist
needs fifty of them, and the warm-up that seeds them at boot was off by default and
returned in silence. So the specialist was blind for the first fifty minutes of every
session and after every restart, and no log anywhere said so.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from quant_ai.agents.swarm import (
    TECHNICAL_MINIMUM_BARS,
    AgentAnalysisRequest,
    TechnicalQuantAgent,
)
from quant_ai.daemon import WARM_INTRADAY_BARS, DaemonRunner
from quant_ai.domain.models import AssetClass, Market
from quant_ai.intelligence.pipeline import TECHNICAL_MINIMUM_BARS as PIPELINE_MINIMUM_BARS
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 22, 9, 20, tzinfo=timezone.utc)


def _closes(count: int) -> tuple[Decimal, ...]:
    """A gently rising series, so real metrics are non-neutral when enough bars exist."""
    return tuple(Decimal(100) + Decimal(i) / Decimal(10) for i in range(count))


def test_the_three_thresholds_are_one_number() -> None:
    """The warm-up decides "ready" on the number the specialist actually requires.

    Three modules hold this bound: the pipeline that computes the metrics, the specialist
    that refuses to read defaults, and the warm-up that has to leave enough behind. Were
    the warm-up's copy to drift below the specialist's, it would report "ready" over a
    feed that still blinds the agent, which is the failure this file exists for.
    """
    assert PIPELINE_MINIMUM_BARS == WARM_INTRADAY_BARS
    assert TECHNICAL_MINIMUM_BARS == Decimal(WARM_INTRADAY_BARS)


def test_below_the_bound_the_metrics_are_defaults_and_the_specialist_abstains() -> None:
    """Fewer bars than the bound means the agent is reading placeholders, not measurements."""
    short = SwarmMarketAnalysisPipeline._technical_metrics(_closes(PIPELINE_MINIMUM_BARS - 1))
    # Exactly the values seen in production: an RSI of precisely 50 with zero spread and
    # zero momentum is a default, never a measurement.
    assert short["rsi"] == Decimal(50)
    assert short["sma_spread"] == Decimal(0)
    assert short["momentum"] == Decimal(0)

    evidence = TechnicalQuantAgent().analyze(
        AgentAnalysisRequest("TRENT", Market.INDIA, AssetClass.EQUITY, NOW, dict(short))
    )
    assert evidence.confidence == Decimal(0)
    assert "insufficient_price_history" in evidence.rationale


def test_at_the_bound_the_specialist_holds_a_stance() -> None:
    """One more bar is the difference between the swarm's most confident voter and silence."""
    warm = SwarmMarketAnalysisPipeline._technical_metrics(_closes(PIPELINE_MINIMUM_BARS))
    assert warm["rsi"] != Decimal(50) or warm["sma_spread"] != Decimal(0)

    evidence = TechnicalQuantAgent().analyze(
        AgentAnalysisRequest("TRENT", Market.INDIA, AssetClass.EQUITY, NOW, dict(warm))
    )
    assert evidence.confidence > Decimal(0)
    assert "insufficient_price_history" not in evidence.rationale


def test_a_disabled_warm_up_says_so_instead_of_returning_in_silence(caplog) -> None:
    """The silent return is what made this cost a session to find rather than a log line."""
    runner = SimpleNamespace(
        intraday_warmup_provider=None,
        intraday_warmup_instruments=(),
        _logger=logging.getLogger("pramana.test.warmup"),
    )
    with caplog.at_level(logging.WARNING, logger="pramana.test.warmup"):
        DaemonRunner._warm_intraday_history(runner, NOW)
    messages = [record.getMessage() for record in caplog.records]
    assert any("intraday_warmup_disabled" in message for message in messages), messages
    # The number is in the line, so the reader does not have to find it in the source.
    assert any(str(WARM_INTRADAY_BARS) in message for message in messages), messages


def test_the_pilot_deployment_warms_up_and_the_setting_is_documented() -> None:
    """A default of "none" in code is why this was off; the pilot states its own choice."""
    compose = (ROOT / "deploy/docker-compose.yml").read_text()
    ghost = compose.split("  pramana-ghost:", 1)[1].split("  dashboard:", 1)[0]
    assert "PRAMANA_INTRADAY_WARMUP_PROVIDER: ${PRAMANA_INTRADAY_WARMUP_PROVIDER:-kite}" in ghost
    assert "PRAMANA_INTRADAY_WARMUP_PROVIDER=kite" in (ROOT / ".env.example").read_text()
