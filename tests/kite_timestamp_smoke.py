"""Offline protocol/SDK timestamp check; requires the actual pilot SDK dependency."""
import asyncio
import json
import os
import struct
import time
from datetime import datetime, timedelta, timezone
from importlib.metadata import version

from kiteconnect import KiteTicker

from quant_ai.marketdata.ticker_stream import TickBuffer, ZerodhaKiteTicker
from quant_ai.orchestration.cadence import CadenceMarketReader


async def check(zone, size):
    os.environ["TZ"] = zone
    time.tzset()
    stamp = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    packet = bytearray(size)
    token = 1 if size == 184 else 265  # NSE equity / index segment.
    struct.pack_into(">II", packet, 0, token, 10000)
    struct.pack_into(">I", packet, 60 if size == 184 else 28, int(stamp.timestamp()))
    if size == 184:
        struct.pack_into(">I", packet, 16, 100)
    parsed = KiteTicker("synthetic", "synthetic")._parse_binary(struct.pack(">HH", 1, size) + packet)
    buffer = TickBuffer(clock=lambda: stamp + timedelta(minutes=5))
    stream = ZerodhaKiteTicker("synthetic", "synthetic", [token], {token: "INFY"}, buffer)
    stream._loop = asyncio.get_running_loop()
    stream._on_ticks(None, parsed)
    await asyncio.sleep(.01)
    assert buffer.latest("INFY").observed_at == stamp
    assert CadenceMarketReader(buffer).market_data_status("INFY", stamp + timedelta(minutes=5))[1] == "Stale Market Data"
    return {"hostTimezone": zone, "packetBytes": size, "exchangeTimestamp": stamp.isoformat(), "staleVeto": True}


async def main():
    original = os.environ.get("TZ")
    try:
        results = [await check(zone, size) for zone in ["UTC", "Asia/Kolkata", "America/New_York"] for size in [184, 32]]
        print(json.dumps({"sdkTimestampVerification": "pass", "kiteconnect": version("kiteconnect"),
                          "cases": results, "networkCalls": 0}))
    finally:
        if original is None: os.environ.pop("TZ", None)
        else: os.environ["TZ"] = original
        time.tzset()


if __name__ == "__main__":
    asyncio.run(main())
