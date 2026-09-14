"""Isolated Docker smoke fixture. Never starts provider streams or AI analysis.

Mounted into the image by CI; not a daemon mode or a deployment entrypoint.
"""
import argparse
import json
import os
import signal
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from deployment_research_fixture import build as build_research

from quant_ai.daemon import _env_holidays, build_ghost_runner
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.governance.directives import FounderDirectives
from quant_ai.marketdata.ticker_stream import LiveTick


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    root = args.state
    tenant = os.environ.get("PRAMANA_TENANT_ID", "pilot")
    ledger = Path(os.environ.get("PRAMANA_LEDGER_PATH", root / "ledger.db"))
    proofs = Path(os.environ.get("PRAMANA_PROOF_DIR", root / "proofs"))
    halt = Path(os.environ.get("PRAMANA_HALT_FILE", root / "HALT"))
    market_path = Path(os.environ.get("PRAMANA_MARKET_SNAPSHOT", root / "market.json"))
    if not (root / "research").exists():
        build_research(root / "research", tenant)
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    runner = build_ghost_runner(
        zerodha_api_key="synthetic", zerodha_access_token="synthetic", zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: "INFY"}, ib_client=SimpleNamespace(), ib_contracts=(),
        include_ibkr=False, database=ledger, tenant_id=tenant,
        log_path=root / "events.jsonl", xai_directory=proofs, halt_file=halt,
        directives=FounderDirectives.from_env() or FounderDirectives(watchlist=(instrument,)), pilot_mode=True,
        holidays=_env_holidays(),
    )
    broker = runner.daemon.tracker.broker
    if not broker.ledger_entries(tenant):
        broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 2, Decimal(100),
                              "isolated_container_fixture", tenant_id=tenant, stop_price=Decimal(95)))
    runner.daemon._reconcile_pilot()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        while not stopped.is_set():
            mode = (root / "fixture-mode").read_text().strip() if (root / "fixture-mode").exists() else "fresh"
            if mode != "freeze":
                now = datetime.now(timezone.utc)
                runner.daemon.tracker.market_feed.buffer.put(
                    LiveTick("INFY", Decimal(100), Decimal(100), None, None, now, "isolated_container_fixture"))
                runner.daemon.protection_tick(now)
                market = {"status": "ok", "source": "Synthetic container verification only",
                          "fetchedAt": now.isoformat(), "rows": [{"symbol": "INFY", "available": True,
                          "price": 100, "exchangeTimestamp": now.isoformat(), "history": []}]}
                temporary = market_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(market))
                temporary.replace(market_path)
            (root / "fixture-observed-mode").write_text(mode)
            stopped.wait(.5)
    finally:
        broker._connection.close()


if __name__ == "__main__":
    main()
