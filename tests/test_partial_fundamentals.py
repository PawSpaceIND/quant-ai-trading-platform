"""Valuation specialists score the ratios they were given and say what they were not.

On 21 September 2026 Yahoo returned no ``freeCashflow`` for eight of the pilot's nine NSE
equities and no trailing P/E for INDIGO or the metal ETFs. The provider then abstained on
every one of them and the Indian-equities specialist was silent on the whole book. These
tests pin the replacement contract: an absent ratio is never a penalty, coverage scales
confidence, and fewer than two ratios is an explicit abstention.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.contracts import Stance
from quant_ai.agents.swarm import (
    INDIA_VALUATION_RATIOS,
    MIN_VALUATION_RATIOS,
    US_VALUATION_RATIOS,
    VALUATION_CONFIDENCE,
    AgentAnalysisRequest,
    IndianEquitiesAgent,
    USEquitiesAgent,
)
from quant_ai.domain.models import AssetClass, Market

NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
INDIA_FULL = {
    "pe": Decimal(22),
    "debt_equity": Decimal("0.3"),
    "operating_margin": Decimal("0.2"),
    "fcf_yield": Decimal("0.03"),
}
US_FULL = {"pe": Decimal(28), "operating_margin": Decimal("0.25"), "fcf_yield": Decimal("0.03")}
INDIA_BASE = "india_valuation_balance_sheet_margin_and_news"
US_BASE = "us_tech_valuation_margin_fcf_and_rates"


def india(metrics: dict, asset_class: AssetClass = AssetClass.EQUITY):
    request = AgentAnalysisRequest("TRENT", Market.INDIA, asset_class, NOW, dict(metrics), 0)
    return IndianEquitiesAgent().analyze(request)


def us(metrics: dict):
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, NOW, dict(metrics), 0)
    return USEquitiesAgent().analyze(request)


def score_of(item) -> Decimal:
    return item.expected_return / Decimal("0.04")


def without(full: dict, *names: str) -> dict:
    return {key: value for key, value in full.items() if key not in names}


def test_ratio_sets_and_floor_are_the_documented_ones() -> None:
    assert INDIA_VALUATION_RATIOS == ("pe", "debt_equity", "operating_margin", "fcf_yield")
    assert US_VALUATION_RATIOS == ("pe", "operating_margin", "fcf_yield")
    assert MIN_VALUATION_RATIOS == 2
    assert VALUATION_CONFIDENCE == Decimal("0.80")


def test_full_coverage_reads_exactly_as_before() -> None:
    item = india(INDIA_FULL)
    assert score_of(item) == Decimal("0.90")
    assert item.confidence == Decimal("0.80")
    assert item.rationale == (INDIA_BASE,)
    assert item.stance is Stance.STRONG_BUY


def test_missing_free_cashflow_is_not_scored_and_is_named() -> None:
    # The 21 September shape: three ratios, no free cash flow.
    item = india(without(INDIA_FULL, "fcf_yield"))
    assert score_of(item) == Decimal("0.90") - Decimal("0.15")
    assert item.confidence == Decimal("0.60")
    assert item.rationale == (INDIA_BASE + ";fundamentals=3/4:missing=fcf_yield",)
    assert item.stance is Stance.STRONG_BUY


def test_an_absent_ratio_is_never_a_penalty() -> None:
    absent = score_of(india(without(INDIA_FULL, "fcf_yield")))
    poor = score_of(india({**INDIA_FULL, "fcf_yield": Decimal("0.001")}))
    assert absent == Decimal("0.75")
    assert poor == Decimal("0.70")
    assert absent > poor
    no_pe = score_of(india(without(INDIA_FULL, "pe")))
    loss_maker = score_of(india({**INDIA_FULL, "pe": Decimal(0)}))
    assert no_pe == Decimal("0.60")
    assert loss_maker == Decimal("0.45")


@pytest.mark.parametrize(
    "drop, expected_confidence, expected_missing",
    [
        (("fcf_yield",), Decimal("0.60"), "fcf_yield"),
        (("pe", "fcf_yield"), Decimal("0.40"), "pe,fcf_yield"),
        (("debt_equity", "operating_margin"), Decimal("0.40"), "debt_equity,operating_margin"),
    ],
)
def test_confidence_scales_with_coverage_and_names_the_missing_ratios_in_order(
    drop, expected_confidence, expected_missing
) -> None:
    item = india(without(INDIA_FULL, *drop))
    assert item.confidence == expected_confidence
    assert item.rationale[0] == f"{INDIA_BASE};fundamentals={4 - len(drop)}/4:missing={expected_missing}"


@pytest.mark.parametrize("present", [(), ("pe",), ("operating_margin",)])
def test_fewer_than_two_ratios_is_an_explicit_abstention(present) -> None:
    metrics = {name: INDIA_FULL[name] for name in present}
    metrics["equity_news_sentiment"] = Decimal("0.9")
    item = india(metrics)
    assert item.confidence == Decimal(0)
    assert item.stance is Stance.NEUTRAL
    assert score_of(item) == Decimal(0)
    missing = ",".join(name for name in INDIA_VALUATION_RATIOS if name not in present)
    assert item.rationale == (
        f"india_fundamentals_insufficient;fundamentals={len(present)}/4:missing={missing}",
    )


def test_partial_coverage_still_reads_news_and_the_tape_after_the_coverage_note() -> None:
    metrics = {
        **without(INDIA_FULL, "fcf_yield"),
        "equity_news_sentiment": Decimal("0.5"),
        "india_vix": Decimal(27),
        "fii_net_crore": Decimal(-3000),
    }
    item = india(metrics)
    assert score_of(item) == Decimal("0.75") + Decimal("0.15") - Decimal("0.35") - Decimal("0.15")
    assert item.rationale[0] == (
        INDIA_BASE + ";fundamentals=3/4:missing=fcf_yield;india_vix=27:stressed;fii_net_crore=-3000:outflow"
    )


def test_freshness_haircut_applies_on_top_of_coverage() -> None:
    item = india({**without(INDIA_FULL, "fcf_yield"), "freshness_multiplier": Decimal("0.5")})
    assert item.confidence == Decimal("0.30")


def test_non_decimal_values_do_not_count_as_ratios() -> None:
    item = india({**without(INDIA_FULL, "fcf_yield"), "fcf_yield": "n/a"})
    assert item.confidence == Decimal("0.60")
    assert item.rationale[0].endswith("missing=fcf_yield")


def test_market_and_asset_gates_still_come_first() -> None:
    assert india(INDIA_FULL, AssetClass.COMMODITY).rationale == ("non_equity_instrument",)
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, NOW, {}, 0)
    assert IndianEquitiesAgent().analyze(request).rationale == ("non_india_market",)


def test_us_specialist_follows_the_same_contract_over_three_ratios() -> None:
    full = us({**US_FULL, "us10y": Decimal("4.0")})
    assert full.confidence == Decimal("0.80")
    assert full.rationale == (US_BASE,)
    assert score_of(full) == Decimal("0.85")

    partial = us({**without(US_FULL, "fcf_yield"), "us10y": Decimal("4.0")})
    assert score_of(partial) == Decimal("0.70")
    assert partial.confidence == Decimal("0.80") * Decimal(2) / Decimal(3)
    assert partial.rationale == (US_BASE + ";fundamentals=2/3:missing=fcf_yield",)

    thin = us({"pe": Decimal(28), "us10y": Decimal("4.0"), "equity_news_sentiment": Decimal("0.9")})
    assert thin.confidence == Decimal(0)
    assert thin.stance is Stance.NEUTRAL
    assert thin.rationale == (
        "us_fundamentals_insufficient;fundamentals=1/3:missing=operating_margin,fcf_yield",
    )
