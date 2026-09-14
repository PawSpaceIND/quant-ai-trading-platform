"""Real paper ledger with dated trades; independent Decimal close-mark oracle."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_contribution_fixture import account

from quant_ai.marketdata.risk_history import risk_history_input

NOW = datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Kolkata"))


def fixture(directory: Path, now=NOW):
    sessions = risk_history_input([], now)["calendar"]["sessions"][-31:]
    fill_days = [1, 3, 6, 9, 14, 18]
    times = [datetime.fromisoformat(sessions[i]).replace(hour=10, tzinfo=NOW.tzinfo) for i in fill_days]
    broker, telemetry, _ = account(directory, now, execution_times=times)
    try:
        valuation = telemetry.publish(now)
        rows = []
        for symbol, asset, base in [("TCS", "EQUITY", 100), ("INFY", "EQUITY", 50),
                                    ("NIFTY", "ETF", 80), ("NIFTY 50", "INDEX", 20000),
                                    ("NIFTY BANK", "INDEX", 40000)]:
            observations = []
            for i, date in enumerate(sessions):
                close = Decimal(base) * (1 + Decimal(i) / 1000 + Decimal([0, 2, -1, 1][i % 4]) / 100)
                observations.append({"date": date, "close": float(close)})
            # Distinct benchmark paths make selection meaningful.
            if symbol == "NIFTY BANK":
                for i, o in enumerate(observations):
                    o["close"] = float(Decimal(base) * (1 - Decimal(i) / 2000 + Decimal(i % 3) / 100))
            rows.append({"instrument": {"symbol": symbol, "market": "INDIA", "assetClass": asset,
                                        "currency": "INR", "exchange": "NSE", "providerInstrumentId": symbol},
                         "history": observations})
        history = risk_history_input(rows, now)
        history["source"] = "Synthetic dated paper-account comparison (no external provider)"
        # Independent known fills: price already includes 1% spread and 10bps slippage.
        oracle_trades = [(1, "TCS", 4, "101.1"), (3, "TCS", 2, "121.32"), (6, "TCS", -3, "128.57"),
                         (9, "INFY", 2, "50.55"), (14, "INFY", -2, "44.505"), (18, "NIFTY", 1, "80.88")]
        cash, positions, expected = Decimal(10000), {}, []
        prices = {r["instrument"]["symbol"]: {o["date"]: Decimal(str(o["close"])) for o in r["history"]} for r in rows}
        for i, day in enumerate(sessions):
            fees, count = Decimal(0), 0
            for fill_day, symbol, qty, price in oracle_trades:
                if fill_day != i:
                    continue
                fee = abs(qty) * Decimal(price) / 1000
                cash -= qty * Decimal(price) + fee
                positions[symbol] = positions.get(symbol, 0) + qty
                fees += fee
                count += 1
            equity = cash + sum(qty * prices[symbol][day] for symbol, qty in positions.items())
            expected.append({"date": day, "cash": float(cash), "accountEquity": float(equity), "cashFees": float(fees), "fillCount": count})
        return {"account": dict(broker._connection.execute("SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id='default'").fetchone()),
                "fills": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_ledger ORDER BY id")],
                "costs": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_cost_ledger ORDER BY id")],
                "positions": [dict(r) for r in broker._connection.execute("SELECT * FROM paper_positions ORDER BY symbol")],
                "valuation": valuation, "input": history, "expected": expected}
    finally:
        broker._connection.close()


if __name__ == "__main__":
    import argparse
    import tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-state", type=Path)
    args = parser.parse_args()
    if args.runtime_state:
        args.runtime_state.mkdir(parents=True, exist_ok=False)
        result = fixture(args.runtime_state, datetime.now(timezone.utc))
        (args.runtime_state / "fixture.json").write_text(json.dumps(result))
        (args.runtime_state / "market.json").write_text(json.dumps({"status": "ok", "fetchedAt": result["input"]["asOf"], "rows": [], "riskHistory": result["input"]}))
        print(json.dumps({"fixture": "recorded-account-benchmark", "fills": len(result["fills"]), "days": len(result["expected"]), "externalCalls": 0}))
    else:
        with tempfile.TemporaryDirectory() as folder:
            (Path(__file__).parent / "fixtures" / "account-benchmark.json").write_text(json.dumps(fixture(Path(folder)), indent=2) + "\n")
