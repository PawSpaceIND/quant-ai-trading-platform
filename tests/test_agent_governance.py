from datetime import timedelta
from decimal import Decimal

from quant_ai.geography.opportunity import CountryOpportunity, rank_countries
from quant_ai.governance.founder import FounderPolicy
from quant_ai.orchestration.cadence import DecisionCadence


def test_default_atlas_cadence_is_ten_minutes() -> None:
    assert DecisionCadence().atlas_cycle == timedelta(minutes=10)


def test_large_capital_change_requires_founder() -> None:
    assert FounderPolicy().capital_change_escalation(Decimal("0.20")) is not None
    assert FounderPolicy().capital_change_escalation(Decimal("0.05")) is None


def test_country_ranking_is_risk_adjusted() -> None:
    items = (
        CountryOpportunity("A", Decimal("0.10"), Decimal("0.20"), Decimal(1), Decimal(1), Decimal(1)),
        CountryOpportunity("B", Decimal("0.09"), Decimal("0.10"), Decimal(1), Decimal(1), Decimal(1)),
    )
    assert rank_countries(items)[0].country == "B"
