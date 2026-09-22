"""A hold tells you nothing about why the book held, and there are three different whys.

On 22 September 2026 the swarm produced 444 decisions and 440 holds. INDIGO moved +1.77%
at 14:10 IST and was held with one specialist voting BUY, one voting SELL and six
neutral. Nothing in the existing reports could say that: a hit rate scores the votes that
were cast, and the story there is the six that were not.

A deadlocked book and a silent book both hold, and they want opposite fixes. These tests
pin that the two are told apart, and that the expected-versus-realised gap is a comparison
over rows that carry both numbers rather than a difference between two samples.
"""
from __future__ import annotations

import json

from quant_ai.analytics.scorecards import (
    MINIMUM_GROUP,
    SCHEMA,
    SILENT,
    SPLIT,
    UNANIMOUS,
    playbook_scorecard,
    roster_scorecard,
    roster_shape,
)

NEUTRALS = {f"abstainer-{index}": {"stance": "NEUTRAL", "confidence": "0.40"} for index in range(6)}


def row(agents: dict, *, forward: str | None = "0.0177", regime: str = "ranging",
        playbook: str = "range_trading", stance: str = "NEUTRAL",
        governance: str = "held", probability: str | None = "0.5000",
        drift: str | None = None) -> dict:
    return {
        "regime": regime, "playbook": playbook, "stance": stance, "governance": governance,
        "forecast_probability_up": probability, "forecast_cost_bps": "10",
        "expected_return": "0.01", "expected_risk": "0.02",
        "forward_return_60m": forward, "drift_bps": drift,
        "agents": json.dumps(agents),
    }


# The decision this module was written for.
INDIGO = row({"geopolitical-analyst": {"stance": "BUY", "confidence": "0.62"},
              "indian-equities": {"stance": "SELL", "confidence": "0.58"}, **NEUTRALS})
SILENT_ROW = row({**NEUTRALS})
AGREED = row({"geopolitical-analyst": {"stance": "BUY", "confidence": "0.62"},
              "indian-equities": {"stance": "BUY", "confidence": "0.58"}, **NEUTRALS})


def test_a_deadlock_and_a_silence_are_different_facts_wearing_the_same_hold() -> None:
    """The whole reason this report exists. Both produce NEUTRAL; the fixes are opposite."""
    assert roster_shape(INDIGO) == (SPLIT, 1, 1, 6)
    assert roster_shape(SILENT_ROW) == (SILENT, 0, 0, 6)
    assert roster_shape(AGREED) == (UNANIMOUS, 2, 0, 6)


def test_a_split_is_voters_on_both_sides_and_not_a_weak_lean() -> None:
    """Conviction never enters it. Two voters at 0.05 who disagree are still a deadlock."""
    faint = row({"a": {"stance": "BUY", "confidence": "0.05"},
                 "b": {"stance": "SELL", "confidence": "0.05"}})
    assert roster_shape(faint)[0] == SPLIT
    # And one voter leaning alone is not a deadlock however weakly it leans.
    assert roster_shape(row({"a": {"stance": "BUY", "confidence": "0.05"}}))[0] == UNANIMOUS


def test_each_shape_carries_what_the_market_did_next() -> None:
    """A shape with no cost attached is a curiosity. The forward return is the cost."""
    report = roster_scorecard([INDIGO, SILENT_ROW, AGREED])
    assert report["schema"] == SCHEMA and report["report"] == "roster"
    shapes = report["shapes"]
    assert shapes[SPLIT]["decisions"] == 1
    assert shapes[SPLIT]["forward_return"]["mean"] == "0.0177"
    # Six of eight specialists said nothing on every one of these.
    assert shapes[SILENT]["mean_abstaining_specialists"] == "6"


def test_the_lone_dissenter_is_scored_on_whether_the_market_went_its_way() -> None:
    """Which of the two arguing was right, over the sample rather than over one day."""
    report = roster_scorecard([INDIGO])
    assert report["lone_dissent"] == {
        "geopolitical-analyst": {"dissents": 1, "vindicated": 1},   # BUY, and it rose
        "indian-equities": {"dissents": 1, "vindicated": 0},        # SELL, and it rose
    }


def test_a_specialist_in_the_majority_is_not_a_dissenter() -> None:
    """Two BUYs against one SELL: only the SELL was outvoted."""
    outvoted = row({"a": {"stance": "BUY", "confidence": "0.6"},
                    "b": {"stance": "BUY", "confidence": "0.6"},
                    "c": {"stance": "SELL", "confidence": "0.6"}})
    assert roster_scorecard([outvoted])["lone_dissent"] == {"c": {"dissents": 1, "vindicated": 0}}


def test_a_unanimous_or_silent_decision_produces_no_dissent_at_all() -> None:
    """Nobody was outvoted, so nobody is scored for having been."""
    assert roster_scorecard([AGREED, SILENT_ROW])["lone_dissent"] == {}


def test_a_split_with_no_resolved_outcome_is_counted_but_not_scored() -> None:
    """An unresolved horizon cannot vindicate anyone, and must not quietly count as wrong."""
    report = roster_scorecard([row({"a": {"stance": "BUY", "confidence": "0.6"},
                                    "b": {"stance": "SELL", "confidence": "0.6"}}, forward=None)])
    assert report["shapes"][SPLIT]["decisions"] == 1
    assert report["shapes"][SPLIT]["forward_return"]["count"] == 0
    assert report["lone_dissent"] == {}


def test_a_roster_that_cannot_be_read_is_silent_rather_than_an_exception() -> None:
    """Rows decided before the column existed, and rows written by something else."""
    for agents in (None, "", "not json", "[]", "{}", 7):
        assert roster_shape({"agents": agents})[0] == SILENT
    # A well-formed map with a malformed member drops that member, not the row.
    assert roster_shape({"agents": json.dumps({"a": "BUY", "b": {"stance": "BUY", "confidence": "1"}})}) \
        == (UNANIMOUS, 1, 0, 0)


# ------------------------------------------------------------------ playbook scorecard


def test_the_gap_is_measured_only_over_rows_carrying_both_numbers() -> None:
    """Otherwise it is the difference between two samples that share a regime.

    Half these rows resolved and half did not. A mean of every expected value minus a mean
    of every realised return would compare the whole book against the part of it that
    happened to resolve, and would look like a statement about the regime.
    """
    resolved = [row({**NEUTRALS}, forward="0.0177", probability="0.6500") for _ in range(4)]
    unresolved = [row({**NEUTRALS}, forward=None, probability="0.9500") for _ in range(4)]
    group = playbook_scorecard(resolved + unresolved)["groups"][0]
    assert group["paired_rows"] == 4
    assert group["expected_value"]["count"] == 8 and group["realised_return"]["count"] == 4
    # p=0.65 gives EV -0.001500 against a realised +0.0177: a gap of exactly 0.0192.
    assert group["realised_minus_expected"] == "0.019200"


def test_groups_are_the_regime_and_the_playbook_together() -> None:
    """One playbook can be selected by more than one regime; the pair set the floor."""
    report = playbook_scorecard([
        row({**NEUTRALS}, regime="ranging", playbook="range_trading"),
        row({**NEUTRALS}, regime="trending_up", playbook="trend_following"),
        row({**NEUTRALS}, regime=None, playbook=None),
    ])
    assert [(item["regime"], item["playbook"]) for item in report["groups"]] == [
        ("ranging", "range_trading"), ("trending_up", "trend_following"), ("unread", "none"),
    ]


def test_a_thin_group_is_flagged_rather_than_quietly_concluded_from() -> None:
    thin = playbook_scorecard([row({**NEUTRALS})] * (MINIMUM_GROUP - 1))["groups"][0]
    assert thin["thin"] is True and thin["decisions"] == MINIMUM_GROUP - 1
    enough = playbook_scorecard([row({**NEUTRALS})] * MINIMUM_GROUP)["groups"][0]
    assert enough["thin"] is False


def test_holds_fills_and_drift_are_counted_per_regime() -> None:
    report = playbook_scorecard([
        row({**NEUTRALS}, stance="NEUTRAL", governance="held", drift="8.0"),
        row({**NEUTRALS}, stance="BUY", governance="filled", drift="12.0"),
        row({**NEUTRALS}, stance="BUY", governance="filled", drift="16.0"),
    ])["groups"][0]
    assert (report["decisions"], report["holds"], report["fills"]) == (3, 1, 2)
    assert report["median_abs_drift_bps"] == "12.0"


def test_a_row_with_no_payoff_contributes_no_expected_value_rather_than_a_zero() -> None:
    """Zero is a claim that the bet was exactly fair. Absence is not that claim."""
    group = playbook_scorecard([row({**NEUTRALS}, probability=None)])["groups"][0]
    assert group["expected_value"] == {"count": 0, "mean": None, "median": None}
    assert group["realised_minus_expected"] is None and group["paired_rows"] == 0


def test_both_reports_are_deterministic_over_their_inputs() -> None:
    """Two readers of one journal must never disagree about what it says."""
    rows = [INDIGO, SILENT_ROW, AGREED, row({**NEUTRALS}, regime="trending_up")]
    assert playbook_scorecard(rows) == playbook_scorecard(list(rows))
    assert roster_scorecard(rows) == roster_scorecard(list(rows))
    # Sorted, not insertion-ordered: a shuffled journal reads the same.
    assert (playbook_scorecard(rows)["groups"] == playbook_scorecard(rows[::-1])["groups"])


def test_the_scorecards_return_numbers_and_touch_nothing() -> None:
    """The brief is explicit: no mid-session reweight. These cannot reweight anything.

    The guard that matters is that the rows handed in come back unmodified - a report
    that edited its input would be writing to the journal by another name.
    """
    rows = [dict(INDIGO), dict(SILENT_ROW)]
    before = json.dumps(rows, sort_keys=True)
    playbook_scorecard(rows)
    roster_scorecard(rows)
    assert json.dumps(rows, sort_keys=True) == before
