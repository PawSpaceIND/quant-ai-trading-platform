"""Deterministic synthetic broker GET responses; no credentials or network calls."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.execution.broker_observation import capture_kite, inspect_capture


def fixture(now):
    local = now.astimezone(ZoneInfo("Asia/Kolkata"))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Kite's daily book cannot contain yesterday's executions. Compress only
    # this synthetic timeline near midnight; keep production validation strict.
    span = min(600, (local-day_start).total_seconds())
    date = local-timedelta(seconds=span)
    orders, trades = [], []
    for i in range(14):
        base = {"order_id":f"synthetic-{i+1}", "instrument_token":738561+i, "tradingsymbol":"RELIANCE" if i==0 else "INFY" if i==12 else "TCS" if i==13 else f"SYMBOL{i+1}",
            "exchange":"NSE", "product":"CNC", "transaction_type":"BUY", "quantity":3, "exchange_order_id":f"ex-{i+1}"}
        orders.append({**base,"status":"OPEN" if i==12 else "CANCELLED" if i==13 else "COMPLETE", "variety":"regular", "filled_quantity":1 if i>=12 else 3,
            "pending_quantity":2 if i==12 else 0, "cancelled_quantity":2 if i==13 else 0,"average_price":0 if i>=12 else 100,"order_timestamp":date.strftime("%Y-%m-%d %H:%M:%S")})
        trades.append({**base,"trade_id":f"trade-{i+1}-a","quantity":1,"average_price":98,"fill_timestamp":(date+timedelta(seconds=span/10)).strftime("%Y-%m-%d %H:%M:%S")})
        if i<12:
            trades.append({**base,"trade_id":f"trade-{i+1}-b","quantity":2,"average_price":101,"fill_timestamp":(date+timedelta(seconds=span/5)).strftime("%Y-%m-%d %H:%M:%S")})
    class SyntheticReads:
        def get(self, path):
            return {"status":"success","data":{"user_id":"SYNTHETIC"} if path=="/user/profile" else orders if path=="/orders" else trades}
    return capture_kite(SyntheticReads(),"SYNTHETIC","default",clock=lambda:now)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--now",default=None)
    args=parser.parse_args()
    now=datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    report=fixture(now)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({"accountRef":report["accountRef"],"expected":inspect_capture(report),"synthetic":True}))


if __name__=="__main__":
    main()
