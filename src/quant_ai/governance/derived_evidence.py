"""Promotion figures recomputed from a retained ledger, never read from a review artifact.

An operator can type any number into a review document. These functions recompute the six
promotion figures from the decision journal and the daily equity marks inside the ledger
that was pinned as evidence, so the gate weighs what the engine recorded rather than what
the reviewer wrote. Anything that cannot be recomputed raises instead of falling back to a
typed value.

The pinned ledger is copied before it is opened, so the evidence keeps the byte content its
digest was computed over.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from threading import RLock

from quant_ai.analytics.decision_journal import load_rows
from quant_ai.analytics.decision_quality import (
    closed_pnls,
    expectancy_of,
    profit_factor_of,
    profitable_regime_count,
    session_dates,
)
from quant_ai.analytics.metrics import maximum_drawdown
from quant_ai.validation.promotion import StrategyEvidence

EQUITY_TABLE = "risk_daily_equity"


class _JournalReader:
    """The two attributes the journal reader needs, over a throwaway copy of the ledger."""

    def __init__(self, database: Path) -> None:
        self._connection = sqlite3.connect(str(database))
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()

    def close(self) -> None:
        self._connection.close()


def daily_equity_curve(connection: sqlite3.Connection, tenant_id: str) -> tuple[Decimal, ...]:
    """The tenant's recorded daily equity marks, oldest first.

    Empty when the ledger carries no risk-state equity history, which is the case a caller
    has to refuse rather than paper over.
    """
    try:
        rows = connection.execute(
            f"SELECT opening_equity, last_equity FROM {EQUITY_TABLE} "
            "WHERE tenant_id = ? ORDER BY valuation_date",
            (tenant_id,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    if not rows:
        return ()
    curve = [Decimal(str(rows[0]["opening_equity"]))]
    curve.extend(Decimal(str(row["last_equity"])) for row in rows)
    return tuple(curve)


def derive_strategy_evidence(ledger: Path, *, tenant_id: str) -> StrategyEvidence:
    """Recompute the promotion figures from a retained ledger. Refuses what it cannot derive."""
    if not ledger.is_file():
        raise ValueError(f"Retained ledger evidence is not a file: {ledger}")
    with tempfile.TemporaryDirectory() as scratch:
        working = Path(scratch) / "retained-ledger.sqlite"
        shutil.copy2(ledger, working)
        reader = _JournalReader(working)
        try:
            rows = load_rows(reader, tenant_id=tenant_id)
            equity = daily_equity_curve(reader._connection, tenant_id)
        finally:
            reader.close()
    pnls = closed_pnls(rows)
    if not pnls:
        raise ValueError(
            "Cannot derive the trade sample: the retained ledger has no closed trades "
            f"journaled for tenant {tenant_id}"
        )
    expectancy = expectancy_of(pnls)
    profit_factor = profit_factor_of(pnls)
    if profit_factor is None:
        raise ValueError(
            "Cannot derive the profit factor: the retained ledger records no losing trade "
            "to divide by"
        )
    if len(equity) < 2:
        raise ValueError(
            "Cannot derive the maximum drawdown: the retained ledger has fewer than two "
            f"daily equity marks for tenant {tenant_id}"
        )
    return StrategyEvidence(
        len(pnls),
        expectancy,
        maximum_drawdown(equity),
        profit_factor,
        profitable_regime_count(rows),
        len(session_dates(rows)),
    )
