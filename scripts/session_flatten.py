"""Plan the end-of-day flatten, or verify the book went flat. Submits nothing.

Two modes, matching the two halves of :mod:`quant_ai.risk.session_flatten`:

    python3 scripts/session_flatten.py plan   --book book.json
    python3 scripts/session_flatten.py verify --book book.json

``plan`` prints the covering orders that should be sent. ``verify`` prints whether the book
actually ended empty. They are separate commands because they answer separate questions,
and treating the first as the second is how a position survives to the next morning while
the log records a clean close.

**This tool never places an order.** It reads a JSON snapshot, prints a decision, and
stops. The covering orders go through the normal governed OMS like everything else, so they
still pass the risk firewall, idempotency and the ledger. It takes no database path and no
broker credentials for the same reason: a tool that only needs to read a small file should
not be able to trade.

The book file, which the dashboard's account export or five lines of typing can produce::

    {
      "as_of": "2026-09-21T15:15:00+05:30",
      "equity": "100000",
      "positions": {"INFY": 10, "TCS": -5},
      "marks": {"INFY": "1500.50", "TCS": "3200.00"},
      "working_orders": []
    }

``positions`` are signed held units; a negative quantity is short. ``marks`` are current
prices. ``working_orders`` is only read by ``verify`` and is the list of orders still live.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.domain.models import Market, PortfolioSnapshot
from quant_ai.risk.session_flatten import (
    FlattenPolicy,
    plan_session_flatten,
    verify_flat,
)

IST = ZoneInfo("Asia/Kolkata")


class NseSession:
    """The NSE equity session. Holidays are not consulted: this runs on a day the operator
    has already decided is a trading day, and a wrong holiday table would be worse than no
    table at all."""

    timezone = "Asia/Kolkata"

    def closes_on(self, day):
        return time(15, 30), None


class NseCalendar:
    def session(self, market, symbol=None):
        return NseSession()


def _decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as error:
        raise SystemExit(f"{field} is not a number: {value!r}") from error


def load(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("the book file must be a JSON object")

    stated = payload.get("as_of")
    if stated:
        as_of = datetime.fromisoformat(str(stated))
        if as_of.tzinfo is None:
            raise SystemExit(
                "as_of must carry an offset. Where the book sits relative to the close is "
                "the whole question, and a naive instant is wrong half the year."
            )
    else:
        as_of = datetime.now(tz=IST)

    positions = payload.get("positions") or {}
    if not isinstance(positions, dict):
        raise SystemExit("positions must be an object of symbol to signed quantity")
    quantities = {}
    for symbol, quantity in positions.items():
        if not isinstance(quantity, int):
            raise SystemExit(f"{symbol}: position quantity must be a whole number of units")
        quantities[str(symbol)] = quantity

    marks = {
        str(symbol): _decimal(value, f"marks[{symbol}]")
        for symbol, value in (payload.get("marks") or {}).items()
    }
    equity = _decimal(payload.get("equity", 0), "equity")
    if equity <= 0:
        raise SystemExit("equity must be positive")

    exposure = {
        symbol: abs(quantity) * marks.get(symbol, Decimal(0))
        for symbol, quantity in quantities.items()
    }
    portfolio = PortfolioSnapshot(
        equity=equity,
        daily_realized_pnl=Decimal(0),
        gross_exposure=sum(exposure.values(), Decimal(0)),
        symbol_exposure=exposure,
        symbol_quantity=quantities,
    )
    working = tuple(payload.get("working_orders") or ())
    return portfolio, marks, as_of, working


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "verify"))
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--cutoff-minutes", type=int, default=15,
                        help="how long before the 15:30 close the flatten window opens")
    args = parser.parse_args(argv)

    portfolio, marks, as_of, working = load(args.book)

    if args.mode == "plan":
        plan = plan_session_flatten(
            portfolio,
            market=Market.INDIA,
            now=as_of,
            calendar=NseCalendar(),
            marks=marks,
            policy=FlattenPolicy(cutoff=timedelta(minutes=args.cutoff_minutes)),
        )
        print(json.dumps(plan.as_evidence(), indent=2))
        for reason in plan.reasons:
            print(f"  {reason}", file=sys.stderr)
        if plan.verdict == "ready":
            print(f"\nSubmit these {len(plan.orders)} orders through the normal OMS, then "
                  "re-run with 'verify'.", file=sys.stderr)
            return 0
        if plan.verdict in {"already_flat", "not_due"}:
            return 0
        print("\nThis plan does not cover the whole book. Do not treat submitting it as "
              "closing the day.", file=sys.stderr)
        return 1

    proof = verify_flat(portfolio, working_orders=working, checked_at=as_of)
    print(json.dumps(proof.as_evidence(), indent=2))
    for reason in proof.reasons:
        print(f"  {reason}", file=sys.stderr)
    if proof.halt_required:
        print("\nHALT. Positions or orders remain past the flatten; this is unplanned "
              "overnight exposure, not a closed session.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
