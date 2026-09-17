"""Create disposable browser stores with real classes; no fills or provider calls."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.runtime_identity import configure_runtime_identity
from quant_ai.orders.intent import InstrumentBoundOrderIntent
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState


def seed(directory: Path) -> None:
    if (not directory.is_dir() or directory.is_symlink()
            or not directory.name.startswith("pramana-browser-oms-") or any(directory.iterdir())):
        raise ValueError("browser_fixture_requires_new_disposable_directory")
    now = datetime.now(timezone.utc)
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    broker = PaperBrokerService(directory / "paper.sqlite", starting_capital=Decimal(100000))
    oms = DurableOms(directory / "oms.sqlite")
    try:
        broker.get_starting_capital("default")
        configure_runtime_identity(broker, (instrument,), "default", "bound_v1", oms.path, oms=oms)
        order = InstrumentBoundOrderIntent("INFY", Market.INDIA, Side.BUY, 10, Decimal(100),
            "synthetic-browser-only", tenant_id="default", stop_price=Decimal(95),
            take_profit_price=Decimal(110), instrument=instrument)
        created = oms.create(order, decision_id="synthetic-browser-observation", now=now)
        oms.transition(created.client_order_id, OrderState.RISK_APPROVED, now=now)
        oms.transition(created.client_order_id, OrderState.SUBMISSION_UNCERTAIN,
                       reason="synthetic fixture; no order dispatched", now=now)
        foreign = InstrumentBoundOrderIntent("INFY", Market.INDIA, Side.BUY, 1, Decimal(100),
            "OTHER-TENANT-PRIVATE", tenant_id="other-tenant", instrument=instrument)
        oms.create(foreign, decision_id="other-tenant", now=now)
        assert broker.ledger_entries("default") == ()
    finally:
        oms.close()
        broker.close()


if __name__ == "__main__":
    seed(Path(sys.argv[1]))
