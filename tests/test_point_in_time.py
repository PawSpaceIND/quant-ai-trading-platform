"""The universe has to remember what it lost, and the past has to stay ignorant of the future."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.marketdata.point_in_time import (
    BarRevision,
    Listing,
    PointInTimeUniverse,
    close_known_at,
)

START = date(2015, 1, 1)
END = date(2025, 1, 1)
UTC = timezone.utc


def universe(listings: list[Listing]) -> PointInTimeUniverse:
    return PointInTimeUniverse(listings, source="test", coverage_from=START, coverage_to=END)


# ------------------------------------------------------------------ membership through time


def test_a_delisted_name_was_still_tradeable_before_it_failed() -> None:
    book = universe([
        Listing("ALIVE", "INDIA", date(2015, 1, 1)),
        Listing("FAILED", "INDIA", date(2015, 1, 1), date(2019, 6, 1), "insolvency"),
    ])
    assert book.members_on(date(2018, 1, 1)) == ("ALIVE", "FAILED")
    assert book.members_on(date(2020, 1, 1)) == ("ALIVE",)
    # The last tradeable day is the day before delisting, not the delisting day itself.
    assert book.was_tradeable("FAILED", date(2019, 5, 31)) is True
    assert book.was_tradeable("FAILED", date(2019, 6, 1)) is False


def test_a_name_is_absent_before_it_listed() -> None:
    book = universe([
        Listing("OLD", "INDIA", date(2015, 1, 1)),
        Listing("IPO", "INDIA", date(2021, 3, 15)),
    ])
    assert book.members_on(date(2020, 1, 1)) == ("OLD",)
    assert book.members_on(date(2022, 1, 1)) == ("IPO", "OLD")


def test_asking_outside_coverage_refuses_rather_than_returning_empty() -> None:
    book = universe([Listing("ONE", "INDIA", START)])
    with pytest.raises(ValueError, match="outside the universe's coverage"):
        book.members_on(date(2014, 6, 1))
    with pytest.raises(ValueError, match="outside the universe's coverage"):
        book.members_on(date(2026, 6, 1))


def test_a_listing_cannot_be_delisted_before_it_listed() -> None:
    with pytest.raises(ValueError, match="cannot be delisted before it listed"):
        Listing("BROKEN", "INDIA", date(2020, 1, 1), date(2019, 1, 1))


# -------------------------------------------------------------------------- survivorship


def test_a_universe_with_no_failures_is_called_out() -> None:
    book = universe([Listing(f"S{i:03d}", "INDIA", START) for i in range(200)])
    audit = book.audit()
    assert audit.verdict == "survivor_only"
    assert audit.usable_for_research is False
    assert audit.delisted == 0
    assert "still exist" in audit.reasons[0]


def test_an_implausibly_low_failure_rate_is_called_out() -> None:
    listings = [Listing(f"S{i:03d}", "INDIA", START) for i in range(500)]
    listings[0] = Listing("S000", "INDIA", START, date(2019, 6, 1), "merger")
    audit = universe(listings).audit()
    # One failure in five hundred names over ten years is about 0.02% a year.
    assert audit.verdict == "implausibly_clean"
    assert audit.usable_for_research is False


def test_a_believable_failure_rate_passes() -> None:
    listings = [
        Listing(f"S{i:03d}", "INDIA", START, date(2019, 6, 1) if i < 12 else None)
        for i in range(200)
    ]
    audit = universe(listings).audit()
    assert audit.verdict == "plausible"
    assert audit.usable_for_research is True
    assert audit.annual_delisting_rate > 0.005
    assert audit.as_evidence()["delisted"] == 12


def test_the_audit_publishes_its_own_limitation() -> None:
    listings = [
        Listing(f"S{i:03d}", "INDIA", START, date(2019, 6, 1) if i < 12 else None)
        for i in range(200)
    ]
    evidence = universe(listings).audit().as_evidence()
    assert "not proof" in evidence["limitation"]


# ------------------------------------------------------------------------------ lookahead


def revisions() -> list[BarRevision]:
    day = date(2024, 5, 10)
    return [
        BarRevision("INFY", day, Decimal("1450.00"), datetime(2024, 5, 10, 12, 0, tzinfo=UTC), "live"),
        BarRevision("INFY", day, Decimal("1452.25"), datetime(2024, 5, 11, 3, 0, tzinfo=UTC), "eod"),
        BarRevision("INFY", day, Decimal("1448.90"), datetime(2024, 6, 1, 0, 0, tzinfo=UTC), "adjusted"),
    ]


def test_a_later_revision_is_not_visible_earlier() -> None:
    day = date(2024, 5, 10)
    at_close = close_known_at(revisions(), symbol="INFY", trading_day=day,
                              as_of=datetime(2024, 5, 10, 18, 0, tzinfo=UTC))
    next_morning = close_known_at(revisions(), symbol="INFY", trading_day=day,
                                  as_of=datetime(2024, 5, 11, 9, 0, tzinfo=UTC))
    today = close_known_at(revisions(), symbol="INFY", trading_day=day,
                           as_of=datetime(2025, 1, 1, 0, 0, tzinfo=UTC))
    assert at_close == Decimal("1450.00")
    assert next_morning == Decimal("1452.25")
    assert today == Decimal("1448.90")


def test_nothing_known_yet_is_none_not_the_modern_value() -> None:
    day = date(2024, 5, 10)
    assert close_known_at(revisions(), symbol="INFY", trading_day=day,
                          as_of=datetime(2024, 5, 10, 6, 0, tzinfo=UTC)) is None


def test_a_naive_instant_is_refused_because_it_cannot_be_ordered() -> None:
    # DTZ001 is suppressed twice here on purpose: constructing the naive datetime IS the test.
    # A tz-naive instant cannot be compared against an aware one, so a revision carrying one
    # would raise deep inside a backtest rather than at the boundary where it can be fixed.
    naive = datetime(2024, 5, 10, 12, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        BarRevision("INFY", date(2024, 5, 10), Decimal(1), naive, "x")
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        close_known_at(revisions(), symbol="INFY", trading_day=date(2024, 5, 10), as_of=naive)


def test_a_revision_exactly_at_the_cutoff_is_visible() -> None:
    day = date(2024, 5, 10)
    exact = datetime(2024, 5, 11, 3, 0, tzinfo=UTC)
    assert close_known_at(revisions(), symbol="INFY", trading_day=day, as_of=exact) == Decimal("1452.25")
    just_before = exact - timedelta(microseconds=1)
    assert close_known_at(revisions(), symbol="INFY", trading_day=day, as_of=just_before) == Decimal("1450.00")


def test_a_non_positive_close_is_refused() -> None:
    with pytest.raises(ValueError, match="close must be positive"):
        BarRevision("INFY", date(2024, 5, 10), Decimal(0), datetime(2024, 5, 10, tzinfo=UTC), "x")
