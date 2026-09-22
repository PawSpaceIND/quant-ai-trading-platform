"""The entry gate that refuses a fill against a reference the market has left.

On 22 September 2026 five proposals reached this gate in one session and four were refused
by it. The refusals were indistinguishable from one another: the drift was computed inside
the comparison and thrown away, so a session where the market moved twenty-one basis
points looked exactly like one where it moved two hundred. The first is an analysis path a
little too slow; the second is a reference price taken far too early. Only one of those is
worth changing anything for, and nothing recorded which it was.

The tolerance is not touched here, and a test below pins it.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace

from quant_ai.execution.daemon import ENTRY_PRICE_DRIFT_TOLERANCE, AutonomousTradingDaemon

REASON = "pilot_price_moved_during_analysis"


def _gate() -> SimpleNamespace:
    """Only what the refusal itself touches; the surrounding gates are not under test."""
    return SimpleNamespace(_logger=logging.getLogger("pramana.test.drift"))


def _proposal(reference: str) -> SimpleNamespace:
    return SimpleNamespace(symbol="TRENT", decision_id="d-0001", reference_price=Decimal(reference))


def _refuse(reference: str, mark: str, gate: SimpleNamespace | None = None) -> str | None:
    return AutonomousTradingDaemon._entry_price_refusal(gate or _gate(), _proposal(reference), Decimal(mark))


def test_the_tolerance_is_twenty_basis_points_and_this_change_does_not_widen_it() -> None:
    """Recording why a gate refused must never become a reason to refuse less often."""
    assert ENTRY_PRICE_DRIFT_TOLERANCE == Decimal("0.002")


def test_a_move_inside_the_tolerance_is_allowed_and_says_nothing(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        # 10 bps up on a 2838.50 reference.
        assert _refuse("2838.50", "2841.3385") is None
    assert caplog.records == [], "an allowed entry is not an event"


def test_the_boundary_itself_is_allowed_exactly_as_before(caplog) -> None:
    """The original refused on strictly greater than the tolerance; that is preserved."""
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        assert _refuse("1000.00", "1002.00") is None  # exactly +20 bps
        assert _refuse("1000.00", "998.00") is None  # exactly -20 bps
    assert caplog.records == []
    assert _refuse("1000.00", "1002.01") == REASON  # a hair beyond


def test_a_refused_entry_records_how_far_the_price_moved(caplog) -> None:
    """The number that separates "slightly slow" from "far too slow"."""
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        assert _refuse("1000.00", "1005.00") == REASON  # +50 bps
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "pilot_entry_price_drift" in message
    assert "drift_bps=50.0" in message, message
    assert "tolerance_bps=20.0" in message, message
    # The decision it belongs to, so the drift can be joined to the journal's timestamp.
    assert "decision_id=d-0001" in message and "symbol=TRENT" in message, message


def test_a_fall_is_recorded_with_its_sign_rather_than_its_size_alone(caplog) -> None:
    """Which way the market went is the difference between a chase and a reprieve."""
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        assert _refuse("1000.00", "990.00") == REASON
    assert "drift_bps=-100.0" in "\n".join(r.getMessage() for r in caplog.records)


def test_an_unusable_reference_refuses_without_dividing_by_it(caplog) -> None:
    """A zero reference must refuse, not raise: the original short-circuited before the
    division and that ordering is load-bearing."""
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        assert _refuse("0", "1000.00") == REASON
        assert _refuse("-5", "1000.00") == REASON
    # Nothing to report: there is no drift to measure against an unusable reference.
    assert caplog.records == []


def test_a_proposal_missing_its_identifiers_is_still_refused_and_still_measured(caplog) -> None:
    """A refusal path that can raise is worse than one that records nothing.

    The caller expects a reason string, and an exception from the log line would be
    returned to it as neither a refusal nor an approval. Found by an existing test that
    hands this gate a proposal carrying only a reference price.
    """
    bare = SimpleNamespace(reference_price=Decimal("1000.00"))
    with caplog.at_level(logging.WARNING, logger="pramana.test.drift"):
        assert AutonomousTradingDaemon._entry_price_refusal(_gate(), bare, Decimal("1005.00")) == REASON
    message = "\n".join(record.getMessage() for record in caplog.records)
    assert "drift_bps=50.0" in message, message
    assert "symbol=unknown" in message and "decision_id=unknown" in message, message
