"""Push read-only dashboard snapshots to the private Cloudflare site."""
import fcntl
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

CONFIG = Path.home() / ".config/pramana/cloud-web.json"
ROUTES = ("market", "portfolio/mtm", "intelligence/swarm", "execution/friction", "execution/trades")


def publish():
    config = json.loads(CONFIG.read_text())
    snapshots = {}
    for route in ROUTES:
        result = requests.get("http://localhost:3002/api/" + route, timeout=15)
        result.raise_for_status()
        snapshots["/api/" + route] = result.json()
    if snapshots["/api/portfolio/mtm"].get("tenantId") != "india-paper":
        raise ValueError("Source is not the India paper ledger")
    result = requests.post(config["url"] + "/_ingest",
        headers={"Authorization": "Bearer " + config["publish_token"]},
        json={"sourceAt": datetime.now(timezone.utc).isoformat(), "snapshots": snapshots}, timeout=20)
    result.raise_for_status()


if __name__ == "__main__":
    with CONFIG.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                publish()
                print("Paper snapshot published", flush=True)
            except Exception as error:
                print("Snapshot unavailable:", type(error).__name__, flush=True)
            if "--once" in __import__("sys").argv:
                break
            time.sleep(60)
