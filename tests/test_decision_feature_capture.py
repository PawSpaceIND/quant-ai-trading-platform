"""The inputs a decision was made on, recorded beside the conclusions it reached.

The journal has always stored what the agents concluded - stance, confidence, the EV they
claimed - and what happened next: forward returns at four horizons, realised net P&L after
the statutory fee schedule, the exit that fired. What it never stored is the vector those
conclusions were drawn from, although the pipeline computes one on every cadence tick and
hands it to every specialist.

That asymmetry is not recoverable after the fact. Prices can be re-fetched; the macro
series as it read at 11:47, the news sentiment over the headlines then on the wire, and
the freshness state of each provider cannot. Every tick that ran without this is a row
that can never join a training set, which makes the recording - not the modelling - the
part with a deadline on it.

The migration test is the one that matters most: it is the failure that would be silent.
"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.analytics.decision_journal import (
    COLUMNS,
    FEATURE_SCHEMA_VERSION,
    MAX_FEATURES,
    MIGRATIONS,
    SCHEMA,
    TABLE,
    ensure_journal,
    feature_json,
    insert_decision,
    load_rows,
)
from quant_ai.execution.paper_ledger import PaperBrokerService

NOW = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
TENANT = "pilot"


def broker_for(tmp_path):
    return PaperBrokerService(database=str(tmp_path / "ledger.db"))


def row(**overrides):
    base = {
        "decision_id": "d1", "tenant_id": TENANT, "symbol": "INFY", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": NOW.isoformat(), "stance": "BUY",
        "governance": "filled", "agents": "{}",
    }
    base.update(overrides)
    return base


def stored(broker, decision_id="d1"):
    return next(item for item in load_rows(broker, tenant_id=TENANT) if item["decision_id"] == decision_id)


# --------------------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------------------

def legacy_table(path) -> None:
    """A journal exactly as an earlier build left it: the real schema, minus the new columns.

    Built by stripping the added lines out of the shipped ``SCHEMA`` rather than by hand, so
    the fixture carries the same NOT NULL constraints, the same INTEGER on
    ``holding_minutes`` and the same primary key the live ledger has. A hand-rolled
    all-TEXT stand-in would still pass while telling us nothing about the table that
    actually exists on the pilot.
    """
    keep = [c for c in COLUMNS if c not in {name for name, _ in MIGRATIONS}]
    schema = "\n".join(
        line for line in SCHEMA.splitlines()
        if not any(line.strip().startswith(f"{name} ") for name, _ in MIGRATIONS)
    )
    connection = sqlite3.connect(path)
    connection.execute(schema)
    connection.execute(
        f"INSERT INTO {TABLE} ({', '.join(keep)}) VALUES ({', '.join('?' for _ in keep)})",
        tuple(row(decision_id="old").get(c) for c in keep),
    )
    connection.commit()
    connection.close()


def test_a_ledger_written_before_this_change_gains_the_columns_instead_of_breaking(tmp_path):
    """The silent failure this guards.

    ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already exists, so without
    an ALTER the live ledger keeps its old shape while ``insert_decision`` names every
    column in ``COLUMNS``. The insert then raises, the daemon logs it and carries on by
    design, and the only symptom is a journal that quietly stopped growing - on the exact
    table the engine's entire evidence trail depends on.
    """
    path = str(tmp_path / "ledger.db")
    legacy_table(path)
    broker = PaperBrokerService(database=path)

    ensure_journal(broker)
    columns = {r[1] for r in broker._connection.execute(f"PRAGMA table_info({TABLE})")}
    assert {name for name, _ in MIGRATIONS} <= columns

    # The insert that would have raised now succeeds.
    assert insert_decision(broker, row(decision_id="new", features={"rsi": Decimal(55)}))
    assert json.loads(stored(broker, "new")["features"]) == {"rsi": "55"}
    # And the row written before the column existed is still there, with nothing invented.
    assert stored(broker, "old")["features"] is None


def test_the_migration_runs_once_and_is_safe_to_repeat(tmp_path):
    broker = broker_for(tmp_path)
    for _ in range(3):
        ensure_journal(broker)  # every insert calls this; it must never accumulate columns
    names = [r[1] for r in broker._connection.execute(f"PRAGMA table_info({TABLE})")]
    assert len(names) == len(set(names)) == len(COLUMNS)


def test_every_migrated_column_is_nullable(tmp_path):
    """A row decided before a column existed has nothing to put in it.

    A NOT NULL addition would fail outright on a table with rows, and a DEFAULT would
    write a value those decisions never had - which is worse than a gap, because it is a
    gap that reads as data.
    """
    broker = broker_for(tmp_path)
    ensure_journal(broker)
    info = {r[1]: r for r in broker._connection.execute(f"PRAGMA table_info({TABLE})")}
    for name, _ in MIGRATIONS:
        assert info[name][3] == 0, f"{name} is NOT NULL"
        assert info[name][4] is None, f"{name} has a default"


# --------------------------------------------------------------------------------------
# The encoding
# --------------------------------------------------------------------------------------

def test_numbers_are_stored_as_exact_text_and_never_as_floats():
    """A float round-trip changes the number the engine actually saw.

    The pipeline works in Decimal throughout for the same reason the ledger does. A
    training set built from rows that went through a float would be fitting inputs that
    differ from the ones the decision was made on, in the last digits, silently.
    """
    exact = Decimal("0.1234567890123456789")
    encoded = json.loads(feature_json({"rsi": exact}))
    assert encoded["rsi"] == "0.1234567890123456789"
    assert Decimal(encoded["rsi"]) == exact


def test_a_non_finite_value_is_dropped_rather_than_written():
    """NaN in a feature column is indistinguishable from a feature that was absent."""
    encoded = json.loads(feature_json({"ok": Decimal(1), "bad": Decimal("NaN"), "inf": Decimal("Infinity")}))
    assert encoded == {"ok": "1"}


def test_labels_and_flags_keep_their_own_shapes():
    encoded = json.loads(feature_json({"regime": "trending_up", "halted": False, "bars": 50}))
    assert encoded == {"regime": "trending_up", "halted": "false", "bars": "50"}


def test_an_empty_or_unusable_vector_is_no_vector_rather_than_an_empty_one(tmp_path):
    assert feature_json({}) is None
    assert feature_json(None) is None
    assert feature_json("not a mapping") is None
    # A dict of only non-finite values encodes to nothing, which is not a recorded vector.
    assert feature_json({"bad": Decimal("NaN")}) is None


def test_the_vector_is_bounded_so_an_upstream_dict_cannot_grow_the_ledger():
    encoded = json.loads(feature_json({f"f{index}": Decimal(index) for index in range(MAX_FEATURES * 3)}))
    assert len(encoded) == MAX_FEATURES


def test_the_schema_version_is_written_only_when_there_is_a_vector_to_version(tmp_path):
    """The version says which feature set a row belongs to, so a later set is not assumed.

    A row with no features must carry no version: "recorded under schema 1 and empty" and
    "recorded before features existed" are different facts, and only one of them means the
    pipeline had nothing to say.
    """
    broker = broker_for(tmp_path)
    insert_decision(broker, row(decision_id="with", features={"rsi": Decimal(55)}))
    insert_decision(broker, row(decision_id="without"))
    assert stored(broker, "with")["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert stored(broker, "without")["feature_schema_version"] is None
    assert stored(broker, "without")["features"] is None


def test_a_raw_mapping_at_the_writer_is_encoded_rather_than_refused(tmp_path):
    """SQLite cannot bind a dict, and the daemon's journal guard swallows the raise.

    ``decision_row`` encodes the vector, so the cadence path never hits this. A caller
    assembling a row by hand naturally puts the mapping in the column named ``features``,
    and that single mistake would cost every subsequent row while logging once per tick.
    """
    broker = broker_for(tmp_path)
    assert insert_decision(broker, row(decision_id="raw", features={"rsi": Decimal(55)}))
    written = stored(broker, "raw")
    assert json.loads(written["features"]) == {"rsi": "55"}
    assert written["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    # An unusable mapping leaves both columns empty rather than a version with no vector.
    assert insert_decision(broker, row(decision_id="empty", features={"bad": Decimal("NaN")}))
    assert stored(broker, "empty")["features"] is None
    assert stored(broker, "empty")["feature_schema_version"] is None


# --------------------------------------------------------------------------------------
# The wiring: the vector recorded is the one the agents were given
# --------------------------------------------------------------------------------------

def test_the_pipeline_carries_the_vector_it_hands_every_specialist(tmp_path):
    """Computed on every tick and, until now, discarded once the agents had read it."""
    from test_intelligence_pipeline import instrument, plan, portfolio

    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
    from quant_ai.intelligence.sandbox import (
        SandboxFundamentalDataProvider,
        SandboxMacroIndicatorProvider,
        SandboxNewsSentimentProvider,
    )
    from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed

    broker = PaperBrokerService(tmp_path / "pipe.db", starting_capital=Decimal(100000))
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(
        instrument(), datetime.now(timezone.utc), plan(), portfolio(),
        quantity=10, country="USA", tenant_id="features",
    )
    assert result.features, "the tick was judged on something"
    # The technical block the specialists read, not a placeholder.
    assert {"rsi", "momentum", "sma_spread"} <= set(result.features)
    # The per-agent freshness multiplier belongs to the agent, not to the market vector.
    assert "freshness_multiplier" not in result.features
    # Encodable as written: every value survives the journal round trip.
    assert json.loads(feature_json(result.features))


def test_a_result_with_no_features_still_journals(tmp_path):
    """Nothing in the decision path reads the vector back, so its absence is not a fault."""
    broker = broker_for(tmp_path)
    assert insert_decision(broker, row(decision_id="bare"))
    assert stored(broker, "bare")["features"] is None


def test_decision_row_versions_only_the_rows_that_carry_a_vector(tmp_path):
    """The builder the cadence actually uses, which the writer-level test does not reach.

    ``insert_decision`` only touches these columns when a caller hands it a raw mapping,
    so a fault in ``decision_row`` - the path every real decision takes - is invisible from
    there. A version stamped on a row with no vector claims the pipeline was asked and had
    nothing to say, when in truth it was never recorded: a gap that reads as data.
    """
    from test_decision_journal import execute, pilot_broker, proposal_for

    from quant_ai.analytics import decision_journal as journal
    from quant_ai.domain.models import Side

    broker = pilot_broker(tmp_path, name="rows.sqlite")
    result = execute(broker, proposal_for(Side.BUY, decision_id="with-features"))

    carried = journal.decision_row(
        result, tenant_id="pilot", now=NOW, features={"rsi": Decimal(55)}
    )
    assert json.loads(carried["features"]) == {"rsi": "55"}
    assert carried["feature_schema_version"] == FEATURE_SCHEMA_VERSION

    bare = journal.decision_row(result, tenant_id="pilot", now=NOW)
    assert bare["features"] is None
    assert bare["feature_schema_version"] is None, "an unrecorded vector is not version 1"

    # An unusable vector is the same as none: no version without something to version.
    unusable = journal.decision_row(
        result, tenant_id="pilot", now=NOW, features={"bad": Decimal("NaN")}
    )
    assert unusable["features"] is None and unusable["feature_schema_version"] is None
