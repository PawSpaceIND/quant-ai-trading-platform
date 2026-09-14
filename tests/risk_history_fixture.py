"""Independent orthogonal-return fixture for dashboard risk accounting."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.marketdata.risk_history import risk_history_input


def fixture():
    all_sessions = risk_history_input([], datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Kolkata")))["calendar"]["sessions"]
    last = datetime.fromisoformat(all_sessions[120]).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    now = last + timedelta(days=1, hours=12)
    sessions = risk_history_input([], now)["calendar"]["sessions"]
    assert len(sessions) == 121
    rows = []
    for symbol, changes in [("ALPHA", [".1", "-.1", ".1", "-.1"]),
                            ("BETA", [".1", ".1", "-.1", "-.1"]),
                            ("FLAT", ["0", "0", "0", "0"])]:
        price = Decimal(100)
        history = [{"date": sessions[0], "close": float(price)}]
        for t, day in enumerate(sessions[1:]):
            price *= 1 + Decimal(changes[t % 4])
            history.append({"date": day, "close": float(price)})
        rows.append({"instrument": {"symbol": symbol, "market": "INDIA", "assetClass": "EQUITY",
                                    "currency": "INR", "exchange": "NSE", "providerInstrumentId": symbol},
                     "history": history})
    history = risk_history_input(rows, now)
    history["source"] = "Synthetic orthogonal daily returns (no external provider)"
    portfolio = {"status": "ok", "markMode": "engine_live", "currency": "INR", "markDisclaimer": "Synthetic fixture",
                 "cash": 400, "totalEquity": 1000, "updatedAt": now.isoformat(), "holdings": [],
                 "realizedPnl": 0, "unrealizedPnl": 0, "highWaterMark": 1000, "drawdown": 0, "equityCurve": []}
    for symbol, quantity in [("ALPHA", 3), ("BETA", 2), ("FLAT", 1)]:
        portfolio["holdings"].append({"symbol": symbol, "market": "INDIA", "assetClass": "EQUITY", "quantity": quantity,
                                      "markPrice": 100, "marketValue": quantity * 100, "averageEntry": 100, "unrealizedPnl": 0,
                                      "fresh": True, "markSource": "live_tick", "markTimestamp": now.isoformat()})
    return {"portfolio": portfolio, "input": history,
            "expected": {"intervals": 120, "covarianceDiagonal": float(Decimal("1.2") / 119),
                         "variance": float(Decimal(".156") / 119), "alphaVarianceShare": 9/13,
                         "betaVarianceShare": 4/13, "var95": 50, "es95": 50,
                         "scenarioPnlCycle": [50, -10, 10, -50]}}
