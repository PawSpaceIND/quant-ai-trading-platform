"""Capture selected-account Kite order/trade evidence without broker writes."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_ai.execution.broker_journal import BrokerJournal
from quant_ai.execution.broker_observation import capture_kite, inspect_capture
from quant_ai.execution.live_brokers import ReadOnlyJsonTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="Expected Kite user ID; identity is checked before and after reads")
    parser.add_argument("--tenant-id", required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="New private evidence file; existing files are not overwritten")
    destination.add_argument("--journal", type=Path, help="Append to the selected private observation journal")
    args = parser.parse_args()
    key, token = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
    if not key or not token:
        parser.error("KITE_API_KEY and KITE_ACCESS_TOKEN are required")
    if args.output and args.output.exists():
        parser.error("Output already exists; select a new capture file")
    transport = ReadOnlyJsonTransport("https://api.kite.trade", {
        "X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
    try:
        report = capture_kite(transport, args.account_id, args.tenant_id)
        if args.journal:
            journal=BrokerJournal(args.journal,args.tenant_id,report["accountRef"])
            try:
                receipt=journal.append(report)
            finally:
                journal.close()
        else:
            receipt=None
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(report, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
    except (OSError, ValueError, ArithmeticError, sqlite3.Error):
        # Broker exception bodies can contain private details; do not print them.
        print("Broker capture failed. Check selected account, credentials, connectivity and output storage.", file=sys.stderr)
        return 1
    status = inspect_capture(report)
    print(json.dumps({"sha256": report["sha256"], "status": status["status"],
        "orderCount": status["orderCount"], "tradeCount": status["tradeCount"], "issueCount": status["issueCount"], "journalReceipt":receipt}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
