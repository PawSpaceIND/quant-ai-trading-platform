"""Three marks describe one entry, and the ledger charges against the oldest of them.

The entry gate refuses a proposal whose mark has left ``reference_price`` - the last
closed one-minute bar as of T0 - by more than twenty basis points. The ones it approves
are then filled AT that reference: ``quant_ai.execution.friction`` computes
``execution_price = reference_price +/- spread and slippage``, and no mark observed at
submit enters that arithmetic anywhere.

So an approval at fifteen basis points of drift books fifteen basis points that no broker
would have given, in whichever direction the market happened to move, on every entry the
book takes. The gate's tolerance is an upper bound on the size of that gift.

This module does not change the fill. It records the three marks beside each other so the
amount can be computed per decision instead of argued about, and it pins the claim that
the anchor really is the reference price - so if the anchor ever moves, the constant that
says so fails a test rather than quietly becoming a lie.
"""
from __future__ import annotations

from decimal import Decimal as D

# Sibling test modules by name: pytest puts tests/ on sys.path, the repository root it
# does not (the console script CI runs), so a ``tests.`` package import fails there.
from test_decision_journal import SESSION, TENANT, execute, pilot_broker, proposal_for

from quant_ai.analytics import decision_journal as journal
from quant_ai.domain.models import Side
from quant_ai.execution.daemon import ENTRY_MARK_LIMIT, remember_entry_marks

# 100.00 reference; the book at T0 is 99.95/100.05, exactly ten basis points wide.
SNAPSHOT = {
    "feature_snapshot": {
        "schema": "pramana.feature_snapshot.v1",
        "market": {"last_price": "100.02", "bid": "99.95", "ask": "100.05",
                   "spread_bps": "10.000", "source": "zerodha"},
    }
}
# What the gate measured when the order actually went in: 15 bps above the reference,
# inside the twenty the tolerance allows, so this entry is approved and filled.
GATE = {"mark_at_submit": "100.15", "drift_bps": "15.0"}


class _Fill:
    def __init__(self, price: D | None) -> None:
        self.order_id, self.average_price = "PAPER-1", price


def _proposal(provenance=SNAPSHOT):
    proposal = proposal_for(Side.BUY)
    object.__setattr__(proposal, "provenance", provenance)
    return proposal


def test_the_three_marks_are_recorded_beside_each_other_and_are_not_the_same_number() -> None:
    """The whole point. Any two of them agreeing is a coincidence, not a rule."""
    marks = journal.fill_marks(_proposal(), _Fill(D("100.01")), GATE)
    assert marks["mark_at_t0"] == "100.02"      # the tick the snapshot froze
    assert marks["mark_at_submit"] == "100.15"  # what the book showed at submit
    assert marks["drift_bps"] == "15.0"         # signed, against the reference
    # And the fill was charged against neither of them.
    assert marks["fill_anchor"] == journal.FILL_ANCHOR_REFERENCE == "reference_price_at_t0"


def test_the_book_at_t0_is_carried_through_rather_than_recomputed() -> None:
    """The journal and the proof must never be able to disagree about what was seen."""
    marks = journal.fill_marks(_proposal(), _Fill(D("100.01")), GATE)
    assert (marks["bid"], marks["ask"], marks["spread_bps"]) == ("99.95", "100.05", "10.000")


def test_a_book_with_no_quote_records_nothing_rather_than_a_zero() -> None:
    """"No quote was on the book" and "the spread was zero" are different facts.

    A fill model that read a zero spread would price a touch that was never there, and a
    trainer that could not tell them apart would learn the difference as signal.
    """
    bare = {"feature_snapshot": {"market": {"last_price": "100.02", "bid": None,
                                            "ask": None, "spread_bps": None}}}
    marks = journal.fill_marks(_proposal(bare), _Fill(D("100.01")), GATE)
    assert (marks["bid"], marks["ask"], marks["spread_bps"]) == (None, None, None)
    assert marks["mark_at_t0"] == "100.02"  # the traded price was known and is kept


def test_a_decision_with_no_snapshot_records_nothing_rather_than_failing() -> None:
    """Rows decided before the snapshot existed are still journaled, just emptier."""
    for provenance in (None, {}, {"feature_snapshot": None}, {"feature_snapshot": {"market": "n/a"}}):
        marks = journal.fill_marks(_proposal(provenance), None, None)
        assert marks["mark_at_t0"] is None and marks["bid"] is None
        assert marks["fill_price"] is None and marks["fill_anchor"] is None


def test_the_anchor_is_named_only_when_something_actually_filled() -> None:
    """A refusal has no fill to describe, and must not claim one was priced."""
    refused = journal.fill_marks(_proposal(), None, {"mark_at_submit": "101.0", "drift_bps": "100.0"})
    assert refused["fill_price"] is None and refused["fill_anchor"] is None
    # The drift is still recorded. Refusals alone are a censored sample - every drift
    # above the tolerance and none below it - so both halves have to be kept.
    assert refused["drift_bps"] == "100.0"
    assert journal.fill_marks(_proposal(), _Fill(None), GATE)["fill_anchor"] is None


def test_the_ledger_really_does_charge_against_the_reference_and_not_the_mark(tmp_path) -> None:
    """The claim ``fill_anchor`` makes, proved against the real execution path.

    Two identical proposals journaled with wildly different submit marks. If any observed
    mark entered the pricing, the fills would differ. They do not, because
    ``execution_price`` is a function of ``reference_price`` alone - which is exactly the
    bias these columns exist to expose.
    """
    fills = []
    for index, gate in enumerate(({"mark_at_submit": "100.15", "drift_bps": "15.0"},
                                  {"mark_at_submit": "99.85", "drift_bps": "-15.0"})):
        broker = pilot_broker(tmp_path, name=f"ledger-{index}.sqlite")
        result = execute(broker, proposal_for(Side.BUY, decision_id=f"d-{index}"))
        assert result.fill is not None, "the proposal must actually fill for this to mean anything"
        row = journal.decision_row(result, tenant_id=TENANT, now=SESSION, marks=gate)
        fills.append(row["fill_price"])
        assert row["fill_anchor"] == journal.FILL_ANCHOR_REFERENCE
        assert row["mark_at_submit"] == gate["mark_at_submit"]

    # A market fifteen basis points higher and one fifteen lower are charged identically.
    # That equality IS the finding: no observed mark reaches the pricing.
    assert fills[0] == fills[1]
    # The fill sits off the reference by the modelled friction alone - here 27 bps, the
    # wide thin market assumed when no quote was observed - and matches neither of the
    # two marks the book was actually showing.
    assert D(fills[0]) == D("100.270000")
    assert D(fills[0]) not in (D("100.15"), D("99.85"))


def test_the_new_columns_reach_an_older_ledger(tmp_path) -> None:
    """A ledger written before these columns existed must gain them, not refuse inserts.

    ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, and
    ``insert_decision`` names every column - so without the migration the journal would
    quietly stop growing, which is the one failure the table exists to prevent.
    """
    broker = pilot_broker(tmp_path)
    journal.ensure_journal(broker)
    with broker._lock, broker._connection as db:
        for column in journal.FILL_COLUMNS:
            db.execute(f"ALTER TABLE {journal.TABLE} DROP COLUMN {column}")
    journal.ensure_journal(broker)
    result = execute(broker, proposal_for(Side.BUY, decision_id="migrated"))
    assert journal.record_decision(broker, result, tenant_id=TENANT, now=SESSION, marks=GATE)
    row = journal.load_rows(broker, tenant_id=TENANT)[0]
    assert row["mark_at_submit"] == "100.15" and row["fill_anchor"] == journal.FILL_ANCHOR_REFERENCE


# ----------------------------------------------------------------- the capture itself


class _Runner:
    def __init__(self, store=None) -> None:
        self._entry_marks = {} if store is None else store


def test_the_gate_records_both_numbers_under_the_decision_id() -> None:
    runner = _Runner()
    remember_entry_marks(runner, _proposal(), D("100.15"), D("0.0015"))
    assert runner._entry_marks["decision-1"] == {"mark_at_submit": "100.15", "drift_bps": "15.0"}


def test_nothing_about_the_capture_can_fail_a_submission() -> None:
    """The defect this shape exists to prevent, and the one #234 was hardened against.

    The gate is handed proposals of whatever shape a caller has - the closure suite gives
    it a bare namespace. A capture that raised would leave that caller with an exception
    where it expects a refusal string: told neither yes nor no, which is strictly worse
    than recording nothing. So this is a function, not a method: an attribute lookup on
    ``self`` would raise before any guard inside a method could run.
    """
    class Bare:
        pass

    for runner in (Bare(), _Runner(store="not a dict"), _Runner(store=None), None):
        remember_entry_marks(runner, _proposal(), D("100.15"), D("0.0015"))
    # A proposal with no identifier has nothing to file the measurement under.
    runner = _Runner()
    remember_entry_marks(runner, Bare(), D("100.15"), D("0.0015"))
    assert runner._entry_marks == {}


def test_the_store_is_bounded_so_an_unjournaled_cadence_cannot_grow_it() -> None:
    """Every measurement is popped when its row is journaled. Not every one is journaled.

    An off-hours sweep or a symbol whose decision never reaches the journal leaves its
    measurement behind, so without a bound this would grow for as long as the process runs.
    """
    runner = _Runner()
    for index in range(ENTRY_MARK_LIMIT + 50):
        proposal = _proposal()
        object.__setattr__(proposal, "decision_id", f"d-{index}")
        remember_entry_marks(runner, proposal, D("100.15"), D("0.0015"))
    assert len(runner._entry_marks) == ENTRY_MARK_LIMIT
    # Oldest evicted first: the ones still held are the most recent.
    assert f"d-{ENTRY_MARK_LIMIT + 49}" in runner._entry_marks
    assert "d-0" not in runner._entry_marks


def test_the_gate_itself_hands_the_measurement_over_on_both_outcomes() -> None:
    """The wiring, not just the pieces.

    ``remember_entry_marks`` and ``fill_marks`` each work in isolation; that proves
    nothing about whether the gate ever calls the first one. Removing that single line
    would leave both halves passing and the column permanently null - which is exactly
    the shape of a missing input that looks present.
    """
    import logging
    from types import SimpleNamespace

    from quant_ai.execution.daemon import AutonomousTradingDaemon

    def gate(reference: str, mark: str) -> dict:
        runner = SimpleNamespace(_logger=logging.getLogger("pramana.test.marks"), _entry_marks={})
        proposal = SimpleNamespace(symbol="TRENT", decision_id="d-1",
                                   reference_price=D(reference))
        AutonomousTradingDaemon._entry_price_refusal(runner, proposal, D(mark))
        return runner._entry_marks

    approved = gate("100", "100.15")   # 15 bps: inside the tolerance, fills
    assert approved["d-1"] == {"mark_at_submit": "100.15", "drift_bps": "15.0"}
    refused = gate("100", "100.50")    # 50 bps: refused
    assert refused["d-1"] == {"mark_at_submit": "100.50", "drift_bps": "50.0"}
    # A fall keeps its sign: a market that ran away from a BUY and one that came back to
    # it are different situations wearing the same refusal.
    assert gate("100", "99.50")["d-1"]["drift_bps"] == "-50.0"


def test_every_journal_column_is_declared_in_the_create_table_statement() -> None:
    """The three lists that describe this table must agree, and one of them is easy to miss.

    ``COLUMNS`` is what ``insert_decision`` names, ``MIGRATIONS`` is what an older ledger
    catches up on, and ``SCHEMA`` is what a fresh table is built from. Adding a column to
    the first two and not the third leaves the live path working - ``ensure_journal`` runs
    CREATE then the ALTERs - while any caller that builds a table from ``SCHEMA`` alone
    gets one the insert cannot fill. That is how this was found: eight tests in an
    unrelated module, thirty-eight minutes into a suite run.
    """
    import re

    declared = set(re.findall(r"^\s+(\w+) (?:TEXT|INTEGER)", journal.SCHEMA, re.MULTILINE))
    assert [column for column in journal.COLUMNS if column not in declared] == []
    assert [name for name, _ in journal.MIGRATIONS if name not in declared] == []
    # And the fence is not passing for free: the statement really was parsed.
    assert "decision_id" in declared and len(declared) >= len(journal.COLUMNS)
