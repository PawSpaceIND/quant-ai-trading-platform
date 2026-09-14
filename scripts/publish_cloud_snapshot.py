"""Push read-only dashboard snapshots to the private Cloudflare site."""
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CONFIG = Path.home() / ".config/pramana/cloud-web.json"
ROUTES = ("market", "portfolio/mtm", "intelligence/swarm", "execution/friction", "execution/trades")


def publish():
    config = json.loads(CONFIG.read_text())
    snapshots = {}
    source = "http://localhost:3002"
    client = requests.Session()
    secret = os.environ.get("PRAMANA_SOURCE_DASHBOARD_SECRET", "")
    if secret:
        login = client.post(source + "/api/session", json={"secret": secret}, headers={"Origin": source}, timeout=15)
        login.raise_for_status()
    for route in ROUTES:
        result = client.get(source + "/api/" + route, timeout=15)
        result.raise_for_status()
        snapshots["/api/" + route] = result.json()
    if snapshots["/api/portfolio/mtm"].get("tenantId") != "india-paper":
        raise ValueError("Source is not the India paper ledger")
    workspace = client.get(source + "/api/workspace", timeout=15)
    if workspace.status_code == 200:
        detail = workspace.json()
        if detail.get("tenantId") != "india-paper":
            raise ValueError("Workspace tenant mismatch")
        detail["audit"] = []  # Operator notes remain on the engine host.
        detail.pop("researchLab", None)  # Experiment comparisons stay in the private engine UI.
        detail.pop("companyEvents", None)  # Mapping references and event research remain private.
        detail.pop("researchPortfolio", None)  # Portfolio journals remain private research evidence.
        detail.pop("paperContribution", None)  # Detailed account attribution stays on the engine host.
        snapshots["/api/workspace"] = detail
    elif workspace.status_code != 404:
        workspace.raise_for_status()
    # Bound chart history without changing endpoints/values at retained points.
    portfolios = [snapshots["/api/portfolio/mtm"]]
    if "/api/workspace" in snapshots:
        portfolios.append(snapshots["/api/workspace"]["portfolio"])
    for portfolio in portfolios:
        curve = portfolio.get("equityCurve", [])
        if len(curve) > 240:
            portfolio["equityCurve"] = [curve[round(i * (len(curve) - 1) / 239)] for i in range(240)]
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
            except Exception as error:  # noqa: BLE001 — Keep the publisher loop alive; log no secrets.
                print("Snapshot unavailable:", type(error).__name__, flush=True)
            if "--once" in __import__("sys").argv:
                break
            time.sleep(60)
